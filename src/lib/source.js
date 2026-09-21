import {execFile} from 'node:child_process'
import {readFile} from 'node:fs/promises'
import {promisify} from 'node:util'

const execFileAsync = promisify(execFile)

/**
 * Load raw model data ({families: [...]}) either from a saved JSON snapshot
 * (--source <path>) or by running `devin models list --format json`.
 * Throws a user-friendly error when prices cannot be obtained.
 */
export async function loadModelData({source} = {}) {
  if (source) {
    let raw
    try {
      raw = await readFile(source, 'utf8')
    } catch (error) {
      throw new Error(`Unable to get model prices: cannot read source file "${source}" (${error.code ?? error.message})`)
    }
    try {
      return JSON.parse(raw)
    } catch {
      throw new Error(`Unable to get model prices: "${source}" is not valid JSON (expected \`devin models list --format json\` output)`)
    }
  }

  let stdout
  try {
    ;({stdout} = await execFileAsync('devin', ['models', 'list', '--format', 'json'], {
      maxBuffer: 64 * 1024 * 1024,
    }))
  } catch (error) {
    if (error.code === 'ENOENT') {
      throw new Error('Unable to get model prices: `devin` CLI not found in PATH. Install it or pass --source <file>.')
    }
    const detail = error.stderr?.trim() || error.message
    throw new Error(`Unable to get model prices: \`devin models list\` failed — ${detail}`)
  }

  try {
    return JSON.parse(stdout)
  } catch {
    throw new Error('Unable to get model prices: `devin models list --format json` returned invalid JSON')
  }
}
