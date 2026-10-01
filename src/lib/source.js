import {spawn} from 'node:child_process'
import {open, readFile, unlink} from 'node:fs/promises'
import {tmpdir} from 'node:os'
import {join} from 'node:path'

import {recoverTruncatedJson} from './repair-json.js'

// `devin` can be slow on flaky connections — give the lookup plenty of
// room, but never let devinp hang forever.
const DEVIN_TIMEOUT_MS = 30_000

/**
 * Run `devin models list --format json`, directing its stdout into a
 * temp file.
 *
 * `devin` is an interactive CLI: captured over a pipe, its JSON stream
 * can be truncated mid-write, so the output goes to a real file instead
 * (a file write persists in full). The temp file is always removed in
 * `finally`, on the success and failure paths alike.
 */
export async function runDevinJson({command = 'devin', timeoutMs = DEVIN_TIMEOUT_MS} = {}) {
  const file = join(tmpdir(), `devinp-models-${process.pid}-${Date.now()}.json`)
  const handle = await open(file, 'w')
  try {
    const {code, stderr, timedOut} = await new Promise((resolve, reject) => {
      const child = spawn(command, ['models', 'list', '--format', 'json'], {
        stdio: ['ignore', handle.fd, 'pipe'],
      })
      let stderr = ''
      let timedOut = false
      const timer = setTimeout(() => {
        timedOut = true
        // kill() terminates the process unconditionally even on Windows,
        // where POSIX signals do not exist.
        child.kill('SIGKILL')
      }, timeoutMs)
      child.stderr.on('data', (chunk) => {
        stderr += chunk
      })
      child.on('error', (error) => {
        clearTimeout(timer)
        reject(error)
      })
      child.on('close', (childCode) => {
        clearTimeout(timer)
        resolve({code: childCode, stderr, timedOut})
      })
    })
    if (timedOut) {
      throw new Error(`\`devin models list\` timed out after ${timeoutMs / 1000}s`)
    }
    if (code !== 0) {
      throw new Error(`\`devin models list\` failed — ${stderr.trim() || `exited with code ${code}`}`)
    }
    return await readFile(file, 'utf8')
  } finally {
    // Close before unlink — Windows refuses to delete a file that
    // still has an open handle.
    await handle.close()
    await unlink(file).catch(() => {})
  }
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

/**
 * Load raw model data ({families: [...]}) either from a saved JSON
 * snapshot (--source <path>) or by running `devin models list --format
 * json`. Throws a user-friendly error when prices cannot be obtained.
 * Returns {data, truncated} — `truncated` is true when the devin output
 * was cut off mid-stream and the document was repaired.
 */
export async function loadModelData({source, command = 'devin', timeoutMs = DEVIN_TIMEOUT_MS} = {}) {
  if (source) {
    let raw
    try {
      raw = await readFile(source, 'utf8')
    } catch (error) {
      throw new Error(`Unable to get model prices: cannot read source file "${source}" (${error.code ?? error.message})`)
    }
    try {
      return {data: assertFamilies(JSON.parse(raw), source)}
    } catch (error) {
      if (error.message.startsWith('Unable to get model prices')) throw error
      throw new Error(`Unable to get model prices: "${source}" is not valid JSON (expected \`devin models list --format json\` output)`)
    }
  }

  let raw
  try {
    raw = await runDevinJson({command, timeoutMs})
  } catch (error) {
    if (error.code === 'ENOENT') {
      throw new Error('Unable to get model prices: `devin` CLI not found in PATH. Install it or pass --source <file>.')
    }
    throw new Error(`Unable to get model prices: ${error.message}`)
  }

  try {
    return {data: assertFamilies(JSON.parse(raw), 'devin models list')}
  } catch (error) {
    if (error.message.startsWith('Unable to get model prices')) throw error
    const recovered = recoverTruncatedJson(raw)
    if (recovered != null) {
      return {data: assertFamilies(recovered, 'devin models list (recovered)'), truncated: true}
    }
    const tail = raw.trimEnd().slice(-200)
    throw new Error(
      `Unable to get model prices: \`devin models list --format json\` returned invalid JSON ` +
        `(${error.message} — ${raw.length} bytes, output ends with ${JSON.stringify(tail)}). ` +
        `The output may have been truncated mid-stream: re-run, or save the output ` +
        `with \`devin models list --format json > models.json\` and pass \`--source models.json\`.`,
    )
  }
}
