import {spawn} from 'node:child_process'
import {access, stat} from 'node:fs/promises'
import {constants} from 'node:fs'
import {homedir} from 'node:os'
import {delimiter, isAbsolute, join, normalize} from 'node:path'

/**
 * Find the `devin` that is actually the Devin CLI.
 *
 * Spawning a bare `devin` runs the first executable of that name on PATH,
 * and that is not always the CLI: the Devin desktop app ships an Electron
 * launcher called `devin` (`/Applications/Devin.app/Contents/MacOS/devin`)
 * that ignores `models list --format json`, writes nothing and exits 0.
 * A user whose interactive shell reaches the real CLI through an alias — a
 * thing no child process can see — gets a devinp that runs the launcher
 * instead and fails in a way that looks like a pager problem.
 *
 * So devinp asks each candidate who it is (`--version`, which the real CLI
 * answers with `devin <version> (<build>)`) and uses the first that
 * answers like the CLI. Candidates are every PATH entry in order, then
 * `~/.local/bin` — where the CLI installs itself, and which is often not on
 * PATH. When none answers, the bare command is returned unchanged, so a CLI
 * whose banner changes still gets the old behaviour rather than a refusal.
 */

const VERSION_TIMEOUT_MS = 5_000

// `devin 3000.12.57 (96542e930752)` — the name, then a version number.
const CLI_BANNER = /^\s*devin\s+v?\d/im

/** An executable regular file? Directories and 0644 files do not count. */
async function isExecutableFile(file) {
  try {
    if (!(await stat(file)).isFile()) return false
    await access(file, constants.X_OK)
    return true
  } catch {
    return false
  }
}

/** Candidate paths for `command`, in lookup order, without duplicates. */
function candidatePaths(command, env, home) {
  const dirs = (env.PATH ?? '').split(delimiter).filter(Boolean)
  dirs.push(join(home, '.local', 'bin'))
  const seen = new Set()
  const paths = []
  for (const dir of dirs) {
    if (!isAbsolute(dir)) continue // a relative PATH entry depends on cwd; skip it
    const path = normalize(join(dir, command))
    if (seen.has(path)) continue
    seen.add(path)
    paths.push(path)
  }
  return paths
}

const quote = (text) => JSON.stringify(text.length > 80 ? `${text.slice(0, 79)}…` : text)

/**
 * Ask `file --version`. Resolves `{ok, version}` when it answers like the
 * Devin CLI, else `{ok: false, reason}` — never rejects.
 */
function probeVersion(file, env, timeoutMs) {
  return new Promise((resolve) => {
    let settled = false
    const settle = (result) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      resolve(result)
    }
    let child
    let timer
    try {
      child = spawn(file, ['--version'], {stdio: ['ignore', 'pipe', 'pipe'], env: {...env}})
    } catch (error) {
      settle({ok: false, reason: `could not be started (${error.code ?? error.message})`})
      return
    }
    let stdout = ''
    let stderr = ''
    // Settle on the deadline itself: `close` waits for every holder of the
    // pipes, and a wrapper's orphaned grandchild can keep them open long
    // after the wrapper is killed.
    timer = setTimeout(() => {
      child.kill('SIGKILL')
      child.stdout.destroy()
      child.stderr.destroy()
      settle({ok: false, reason: `timed out after ${timeoutMs / 1000}s answering --version`})
    }, timeoutMs)
    child.stdout.on('data', (chunk) => {
      stdout += chunk
    })
    child.stderr.on('data', (chunk) => {
      stderr += chunk
    })
    child.on('error', (error) => {
      settle({ok: false, reason: `could not be started (${error.code ?? error.message})`})
    })
    child.on('close', (code) => {
      const out = stdout.trim()
      if (code === 0 && CLI_BANNER.test(out)) return settle({ok: true, version: out.split('\n')[0].trim()})
      const said = out || stderr.trim()
      if (!said) return settle({ok: false, reason: `printed nothing for --version (exit ${code})`})
      settle({ok: false, reason: `answered --version with ${quote(said.split('\n')[0])} (exit ${code})`})
    })
  })
}

/**
 * Resolve which executable to run for `command`.
 *
 * Returns `{command, explicit, verified, version, skipped}`:
 * - `command` is what to spawn: an absolute path when a candidate was
 *   verified, otherwise the input unchanged;
 * - `explicit` is true when `command` already contains a path (the caller
 *   chose, nothing is probed);
 * - `skipped` lists the candidates that ranked ahead of the chosen one (or
 *   all of them, when none was verified) with the reason each was refused.
 *
 * Windows is not probed: PATHEXT and `.cmd` shims need a shell to run, and
 * there is no desktop-launcher shadowing known there.
 */
export async function resolveDevin({
  command = 'devin',
  env = process.env,
  home = homedir(),
  timeoutMs = VERSION_TIMEOUT_MS,
  platform = process.platform,
} = {}) {
  const unresolved = {command, explicit: false, verified: false, version: null, skipped: []}
  if (command.includes('/') || command.includes('\\') || isAbsolute(command)) {
    return {...unresolved, explicit: true}
  }
  if (platform === 'win32') return unresolved

  const skipped = []
  for (const path of candidatePaths(command, env, home)) {
    if (!(await isExecutableFile(path))) continue
    const probe = await probeVersion(path, env, timeoutMs)
    if (probe.ok) return {command: path, explicit: false, verified: true, version: probe.version, skipped}
    skipped.push({path, reason: probe.reason})
  }
  return {...unresolved, skipped}
}
