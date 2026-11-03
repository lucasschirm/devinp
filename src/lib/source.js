import {spawn} from 'node:child_process'
import {mkdir, open, readFile, unlink} from 'node:fs/promises'
import {homedir, tmpdir} from 'node:os'
import {join} from 'node:path'

import {recoverTruncatedJson} from './repair-json.js'

// `devin` can be slow on flaky connections — give each lookup plenty of
// room, but never let devinp hang forever. The budget is per attempt.
const DEVIN_TIMEOUT_MS = 30_000

/**
 * The portable "you are not interactive" signal.
 *
 * On some machines `devin` is reached through — or behaves like — an
 * interactive tool: a pager, a TUI that paints onto the terminal instead
 * of stdout, or a wrapper that collapses long output behind a
 * "click to show more". That costs a script the whole document: the CLI
 * exits 0 having written 0 bytes to stdout. These variables are the
 * conventions tools use to opt out of paging, colours and prompts.
 */
const NON_INTERACTIVE_ENV = {
  PAGER: 'cat',
  GIT_PAGER: 'cat',
  TERM: 'dumb',
  NO_COLOR: '1',
  CI: '1',
}

/**
 * Spawn `devin` in its own session (`detached`). On POSIX that is a
 * `setsid()`, which leaves the child with **no controlling terminal**:
 * opening `/dev/tty` fails, so a TUI or pager that paints onto the
 * terminal — behind a "click to show more" — has nowhere to draw and the
 * data has to go to stdout. Ctrl-C in the user's terminal also stops
 * reaching the lookup. On Windows there is no `/dev/tty`; `windowsHide`
 * just keeps the child's console from flashing on screen.
 */
const DETACHED_SPAWN = {detached: true, windowsHide: true}

/**
 * Capture strategies, cheapest first. The first is the healthy path and
 * keeps the caller's environment untouched; the later ones only run when
 * the previous attempt produced nothing usable, and progressively strip
 * the interactive terminal away from `devin`: first the conventions that
 * disable paging/colour, then the controlling terminal itself.
 *
 * A pipe comes first: it needs no scratch file, so an unwritable or
 * sandboxed `TMPDIR` cannot break the lookup (it used to, and cost two
 * extra `devin` runs before a later strategy rescued it). Measured
 * against the real CLI, a piped capture is byte-identical to a file
 * capture — 471 855 bytes, 46 families — so the old "pipes truncate"
 * worry does not apply here; the file strategy stays as the fallback for
 * a CLI that really will not write to a pipe. (For anyone probing this at
 * home: libuv wires `stdio: 'pipe'` with a **socketpair**, so the child's
 * stdout is neither a tty nor a pipe — `[ -p /dev/stdout ]` is false.)
 */
const STRATEGIES = [
  {label: 'pipe', file: false, env: null},
  {label: 'pipe, paging disabled, no controlling terminal', file: false, env: NON_INTERACTIVE_ENV, detached: true},
  {label: 'temp file, paging disabled, no controlling terminal', file: true, env: NON_INTERACTIVE_ENV, detached: true},
]

// CSI colour/cursor sequences plus OSC hyperlinks — some wrappers dress
// up the stream even when the CLI itself is not interactive.
const ANSI = /\u001b\[[0-9;?]*[ -/]*[@-~]|\u001b\][\s\S]*?(?:\u0007|\u001b\\)|\u001b[@-Z\\-_]/g

function stripAnsi(text) {
  return text.replace(ANSI, '')
}

/**
 * Parse a document that may be wrapped in noise (an update banner in
 * front, a "pass a slug to --model" hint behind). Returns null when no
 * complete JSON object can be salvaged.
 */
function parseJsonLoosely(text) {
  const clean = stripAnsi(text)
  try {
    return JSON.parse(clean)
  } catch {
    // fall through and look for the object inside the noise
  }
  const start = clean.indexOf('{')
  const end = clean.lastIndexOf('}')
  if (start >= 0 && end > start) {
    try {
      return JSON.parse(clean.slice(start, end + 1))
    } catch {
      // unrecoverable
    }
  }
  return null
}

/**
 * Spawn `devin models list --format json` and collect its streams. Never
 * rejects, except for a missing executable (`error.code === 'ENOENT'`).
 */
function spawnDevin({command, env, stdout, timeoutMs, detached = false}) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, ['models', 'list', '--format', 'json'], {
      stdio: ['ignore', stdout, 'pipe'],
      ...(env ? {env: {...process.env, ...env}} : {}),
      ...(detached ? DETACHED_SPAWN : {}),
    })
    let stdoutText = ''
    let stderr = ''
    let timedOut = false
    const timer = setTimeout(() => {
      timedOut = true
      // kill() terminates the process unconditionally even on Windows,
      // where POSIX signals do not exist.
      child.kill('SIGKILL')
    }, timeoutMs)
    child.stdout?.on('data', (chunk) => {
      stdoutText += chunk
    })
    child.stderr.on('data', (chunk) => {
      stderr += chunk
    })
    child.on('error', (error) => {
      // Tag spawn failures: ENOENT from here means "no devin binary", while
      // the same code raised by the scratch-file path means "cannot write
      // there" — the two need different advice.
      error.devinSpawn = true
      clearTimeout(timer)
      reject(error)
    })
    child.on('close', (code) => {
      clearTimeout(timer)
      resolve({stdout: stdoutText, stderr, code, timedOut})
    })
  })
}

/**
 * First directory we can actually write a scratch file in.
 *
 * `TMPDIR` is often not a good bet: read-only in sandboxes, redirected to
 * OneDrive or a roaming profile on Windows, missing entirely. Candidates:
 * `DEVINP_TMP`, `os.tmpdir()`, `~/.devinp`, then the current directory.
 * null means no scratch file is possible. Not cached: a caller (or a test)
 * may fix TMPDIR between attempts.
 */
async function writableScratchDir() {
  const candidates = []
  if (process.env.DEVINP_TMP) candidates.push(process.env.DEVINP_TMP)
  candidates.push(tmpdir(), join(homedir(), '.devinp'), process.cwd())
  for (const dir of candidates) {
    try {
      await mkdir(dir, {recursive: true})
      const probe = join(dir, `.devinp-probe-${process.pid}`)
      const handle = await open(probe, 'w')
      await handle.close()
      await unlink(probe)
      return dir
    } catch {
      // try the next candidate
    }
  }
  return null
}

/**
 * Run `devin models list --format json` with stdout wired to a scratch
 * file the child inherits as an fd (a file write persists in full, which
 * is what a pipe cannot promise on every platform). The file is always
 * removed in `finally`, on the success and failure paths alike, and a
 * directory that refuses the write is reported with its path.
 */
async function captureToFile({command, env, timeoutMs, detached}) {
  const dir = await writableScratchDir()
  if (!dir) {
    const error = new Error(`cannot write a scratch file — DEVINP_TMP, os.tmpdir() ("${tmpdir()}"), ~/.devinp and "${process.cwd()}" all refused`)
    error.scratchPath = dir
    throw error
  }
  const file = join(dir, `devinp-models-${process.pid}-${Date.now()}.json`)
  let handle
  try {
    handle = await open(file, 'w')
  } catch (error) {
    error.message = `cannot write scratch file "${file}" (${error.code ?? error.message})`
    error.scratchPath = file
    throw error
  }
  try {
    const result = await spawnDevin({command, env, stdout: handle.fd, timeoutMs, detached})
    // A killed run still leaves whatever it managed to write, which is
    // worth more than nothing on the truncation-repair path.
    result.stdout = await readFile(file, 'utf8')
    return result
  } finally {
    // Close before unlink — Windows refuses to delete a file that
    // still has an open handle.
    await handle.close()
    await unlink(file).catch(() => {})
  }
}

/**
 * Try every capture strategy until one yields a complete JSON document.
 *
 * Returns `{raw, parsed, attempts, timeoutMs}`: `parsed` is null when no
 * attempt produced a parseable document, `raw` is the longest output seen
 * (the best candidate for truncation repair), and `attempts` describes
 * each try so failures can be explained instead of guessed at. Throws on
 * a real `devin` error (non-zero exit with a message on stderr — not
 * signed in, network down) and on a missing executable, because retrying
 * those only burns time.
 */
export async function runDevinJson({command = 'devin', timeoutMs = DEVIN_TIMEOUT_MS} = {}) {
  const attempts = []
  for (const [index, strategy] of STRATEGIES.entries()) {
    let result
    try {
      result = strategy.file
        ? await captureToFile({command, env: strategy.env, timeoutMs, detached: strategy.detached})
        : await spawnDevin({command, env: strategy.env, stdout: 'pipe', timeoutMs, detached: strategy.detached})
    } catch (error) {
      // Only a missing executable is fatal; a scratch-directory problem is
      // just one more failed attempt, recorded with its reason.
      if (error.code === 'ENOENT' && error.devinSpawn) throw error
      attempts.push({label: strategy.label, bytes: 0, code: null, stderr: '', timedOut: false, note: error.message})
      continue
    }
    const raw = result.stdout ?? ''
    const parsed = raw.trim() ? parseJsonLoosely(raw) : null
    attempts.push({
      label: strategy.label,
      bytes: raw.length,
      code: result.code,
      stderr: result.stderr.trim(),
      timedOut: result.timedOut,
      raw,
    })
    if (parsed != null) return {raw, parsed, attempts, timeoutMs, via: strategy.label}
    // A non-zero exit that came with an explanation on the very first try
    // is a real CLI failure (not signed in, network down), not a capture
    // problem — say so instead of retrying. Later attempts only run
    // because the previous one looked like a capture problem, so their
    // stderr goes into the report instead of replacing it.
    if (index === 0 && result.code !== 0 && !result.timedOut && result.stderr.trim()) {
      throw new Error(`\`devin models list\` failed — ${result.stderr.trim()}`)
    }
  }
  const explained = [...attempts].reverse().find((a) => a.stderr)
  if (explained && attempts.every((a) => a.timedOut || a.code !== 0)) {
    throw new Error(`\`devin models list\` failed — ${explained.stderr}`)
  }
  const longest = attempts.reduce((acc, a) => ((a.raw?.length ?? 0) >= acc.length ? a.raw : acc), '')
  return {raw: longest || '', parsed: null, attempts, timeoutMs, via: null}
}

/**
 * Check that a parsed document looks like `devin` output. This keeps
 * devinp from quietly "succeeding" with an empty table.
 */
function assertFamilies(data, sourceName) {
  if (data == null || typeof data !== 'object' || !Array.isArray(data.families)) {
    throw new Error(`Unable to get model prices: ${sourceName} has no "families" array (expected \`devin models list --format json\` output)`)
  }
  if (data.families.length === 0) {
    throw new Error(`Unable to get model prices: ${sourceName} contains no model families`)
  }
  return data
}

function quote(text) {
  return JSON.stringify(text.length > 200 ? `…${text.slice(-199)}` : text)
}

function describeAttempt(attempt, timeoutMs) {
  const parts = [`${attempt.label}: ${attempt.bytes} bytes`]
  if (attempt.timedOut) parts.push(`timed out after ${timeoutMs / 1000}s`)
  else parts.push(`exit ${attempt.code}`)
  if (attempt.stderr) parts.push(`stderr ${quote(attempt.stderr)}`)
  if (attempt.note) parts.push(attempt.note)
  return parts.join(', ')
}

/**
 * Explain why no attempt produced prices. The empty-output case gets its
 * own wording: "invalid JSON (0 bytes)" tells the user nothing, while
 * "the JSON never reached stdout" points straight at the pager/interactive
 * view that ate it, and the fix that always works — capture it yourself.
 */
function describeFailure(attempts, timeoutMs) {
  const bytes = Math.max(...attempts.map((a) => a.bytes), 0)
  const longest = attempts.find((a) => a.bytes === bytes)
  const tail = longest?.raw?.trimEnd().slice(-200) ?? ''
  const lines = []
  if (bytes === 0) {
    lines.push(
      'Unable to get model prices: `devin models list --format json` exited without writing ' +
        'the JSON to stdout (0 bytes). devinp re-ran it with paging disabled and in a ' +
        'session with no controlling terminal, so the CLI had no terminal to paint a ' +
        '"click to show more" view on — whatever swallowed the output sits between ' +
        'devinp and `devin` (a shell function or agent wrapper around it, or a broken install).',
    )
  } else {
    let parseError = ''
    try {
      JSON.parse(stripAnsi(longest.raw))
    } catch (error) {
      parseError = `${error.message} — `
    }
    lines.push(
      `Unable to get model prices: \`devin models list --format json\` returned invalid JSON ` +
        `(${parseError}${bytes} bytes, output ends with ${JSON.stringify(tail)}). ` +
        `The output may have been truncated mid-stream.`,
    )
  }
  lines.push(`Tried: ${attempts.map((a) => describeAttempt(a, timeoutMs)).join('; ')}.`)
  if (attempts.some((a) => a.note?.includes('scratch'))) {
    lines.push('devinp could not write its scratch file — point it somewhere writable with `DEVINP_TMP=/tmp devinp`.')
  }
  lines.push('Capture the JSON yourself and hand it to devinp — piping or redirecting makes `devin` write the whole document in one go:')
  lines.push('  devin models list --format json > models.json && devinp --source models.json')
  lines.push('  devin models list --format json | devinp --source=-')
  lines.push('Check `command -v devin` — if that is not the real CLI, devinp is asking the wrong program. If a plain redirect still prints a paged view, update the CLI (`devin update`).')
  return lines.join('\n')
}

async function readStdin() {
  const chunks = []
  for await (const chunk of process.stdin) chunks.push(chunk)
  return Buffer.concat(chunks.map((c) => (typeof c === 'string' ? Buffer.from(c) : c))).toString('utf8')
}

/**
 * Load raw model data ({families: [...]}) either from a saved JSON
 * snapshot (`--source <path>`, or `--source=-` to read it from stdin) or
 * by running `devin models list --format json`. Throws a user-friendly
 * error when prices cannot be obtained. Returns {data, truncated, retries,
 * via} — `truncated` is true when the devin output was cut off mid-stream
 * and the document was repaired, `retries` is how many extra `devin` runs
 * it took, `via` names the capture strategy that finally worked.
 */
export async function loadModelData({
  source,
  command = 'devin',
  timeoutMs = DEVIN_TIMEOUT_MS,
  readStdinFrom = readStdin,
} = {}) {
  if (source) {
    const label = source === '-' ? 'stdin' : `"${source}"`
    let raw
    try {
      raw = source === '-' ? await readStdinFrom() : await readFile(source, 'utf8')
    } catch (error) {
      throw new Error(`Unable to get model prices: cannot read ${source === '-' ? 'stdin' : `source file "${source}"`} (${error.code ?? error.message})`)
    }
    try {
      return {data: assertFamilies(JSON.parse(raw), label), retries: 0, via: source === '-' ? 'stdin' : 'file'}
    } catch (error) {
      if (error.message.startsWith('Unable to get model prices')) throw error
      throw new Error(`Unable to get model prices: ${label} is not valid JSON (expected \`devin models list --format json\` output)`)
    }
  }

  let outcome
  try {
    outcome = await runDevinJson({command, timeoutMs})
  } catch (error) {
    if (error.code === 'ENOENT' && error.devinSpawn) {
      throw new Error('Unable to get model prices: `devin` CLI not found in PATH. Install it, or pass --source <file> / --source=- to supply the JSON yourself.')
    }
    throw new Error(`Unable to get model prices: ${error.message}`)
  }

  if (outcome.parsed != null) {
    // `retries` and `via` let the CLI be honest about a machine that needed
    // the hardening — surprising output is easier to trust when it is named.
    return {data: assertFamilies(outcome.parsed, 'devin models list'), retries: outcome.attempts.length - 1, via: outcome.via}
  }

  const recovered = recoverTruncatedJson(outcome.raw)
  if (recovered != null) {
    return {data: assertFamilies(recovered, 'devin models list (recovered)'), truncated: true, retries: outcome.attempts.length - 1, via: outcome.via ?? 'repaired'}
  }

  throw new Error(describeFailure(outcome.attempts, outcome.timeoutMs))
}
