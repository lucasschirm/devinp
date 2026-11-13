import assert from 'node:assert/strict'
import {chmod, mkdir, writeFile} from 'node:fs/promises'
import {tmpdir} from 'node:os'
import {join} from 'node:path'
import {describe, it} from 'node:test'

import {resolveDevin} from '../src/lib/resolve-devin.js'
import {loadModelData} from '../src/lib/source.js'

// A name no real machine has, so a developer's own `devin` can never leak
// into these tests through the tail of PATH.
const NAME = 'devin-resolve-test'

async function freshDir() {
  return mkdir(join(tmpdir(), `devinp-resolve-${process.pid}-${Date.now()}-${Math.random().toString(36).slice(2)}`), {recursive: true})
}

/** Executable `NAME` in `dir` whose body is raw shell. */
async function putExecutable(dir, body, name = NAME) {
  const file = join(dir, name)
  await writeFile(file, `#!/bin/sh\n${body}\n`)
  await chmod(file, 0o755)
  return file
}

/** The real CLI: identifies itself and answers `models list`. */
const REAL_CLI = [
  'case "$1" in',
  '  --version) echo "devin 3000.12.57 (96542e930752)"; exit 0 ;;',
  `  models) echo '{"families":[{"slug":"a","variants":[]}]}'; exit 0 ;;`,
  'esac',
].join('\n')

/** The Electron desktop-app launcher: ignores its arguments, writes nothing. */
const ELECTRON_SHIM = `echo "Warning: 'format' is not in the list of known options, but still passed to Electron/Chromium." >&2\nexit 0`

const pathOf = (...dirs) => dirs.join(':')

describe('resolveDevin', () => {
  it('does not probe an explicit path — the caller already chose', async () => {
    const dir = await freshDir()
    const shim = await putExecutable(dir, ELECTRON_SHIM)
    const result = await resolveDevin({command: shim, env: {PATH: ''}, home: dir})
    assert.equal(result.command, shim)
    assert.equal(result.explicit, true)
    assert.deepEqual(result.skipped, [])
  })

  it('uses the first PATH entry when it identifies as the Devin CLI', async () => {
    const [a, b] = [await freshDir(), await freshDir()]
    const real = await putExecutable(a, REAL_CLI)
    await putExecutable(b, REAL_CLI)
    const result = await resolveDevin({command: NAME, env: {PATH: pathOf(a, b)}, home: await freshDir()})
    assert.equal(result.command, real)
    assert.equal(result.verified, true)
    assert.match(result.version, /^devin 3000\.12\.57/)
    assert.deepEqual(result.skipped, [])
  })

  it('skips a silent Electron launcher that shadows the real CLI, and says so', async () => {
    const [shimDir, realDir] = [await freshDir(), await freshDir()]
    const shim = await putExecutable(shimDir, ELECTRON_SHIM)
    const real = await putExecutable(realDir, REAL_CLI)
    const result = await resolveDevin({command: NAME, env: {PATH: pathOf(shimDir, realDir)}, home: await freshDir()})
    assert.equal(result.command, real)
    assert.equal(result.verified, true)
    assert.equal(result.skipped.length, 1)
    assert.equal(result.skipped[0].path, shim)
    assert.match(result.skipped[0].reason, /Warning: 'format' is not in the list/)
  })

  it('describes a launcher that prints nothing at all', async () => {
    const [shimDir, realDir] = [await freshDir(), await freshDir()]
    await putExecutable(shimDir, 'exit 0')
    await putExecutable(realDir, REAL_CLI)
    const result = await resolveDevin({command: NAME, env: {PATH: pathOf(shimDir, realDir)}, home: await freshDir()})
    assert.match(result.skipped[0].reason, /printed nothing for --version \(exit 0\)/)
  })

  it('finds the CLI in ~/.local/bin when PATH does not include it', async () => {
    const [shimDir, home] = [await freshDir(), await freshDir()]
    await putExecutable(shimDir, ELECTRON_SHIM)
    await mkdir(join(home, '.local', 'bin'), {recursive: true})
    const real = await putExecutable(join(home, '.local', 'bin'), REAL_CLI)
    const result = await resolveDevin({command: NAME, env: {PATH: shimDir}, home})
    assert.equal(result.command, real)
    assert.equal(result.verified, true)
    assert.equal(result.skipped.length, 1)
  })

  it('does not probe the same file twice when PATH repeats or includes ~/.local/bin', async () => {
    const home = await freshDir()
    const bin = join(home, '.local', 'bin')
    await mkdir(bin, {recursive: true})
    const shim = await putExecutable(bin, ELECTRON_SHIM)
    const result = await resolveDevin({command: NAME, env: {PATH: pathOf(bin, bin)}, home})
    assert.equal(result.verified, false)
    assert.deepEqual(result.skipped.map((s) => s.path), [shim])
  })

  it('falls back to the bare command when nothing identifies as the CLI', async () => {
    const dir = await freshDir()
    const shim = await putExecutable(dir, ELECTRON_SHIM)
    const result = await resolveDevin({command: NAME, env: {PATH: dir}, home: await freshDir()})
    assert.equal(result.command, NAME)
    assert.equal(result.verified, false)
    assert.deepEqual(result.skipped.map((s) => s.path), [shim])
  })

  it('ignores directories and non-executable files named like the command', async () => {
    const dir = await freshDir()
    await mkdir(join(dir, NAME))
    const other = await freshDir()
    await writeFile(join(other, NAME), '#!/bin/sh\necho "devin 1.0.0"\n') // mode 0644
    const result = await resolveDevin({command: NAME, env: {PATH: pathOf(dir, other)}, home: await freshDir()})
    assert.equal(result.command, NAME)
    assert.deepEqual(result.skipped, [])
  })

  it('reports a candidate that times out instead of waiting on it', async () => {
    const [slowDir, realDir] = [await freshDir(), await freshDir()]
    await putExecutable(slowDir, 'sleep 3')
    const real = await putExecutable(realDir, REAL_CLI)
    const started = Date.now()
    const result = await resolveDevin({command: NAME, env: {PATH: pathOf(slowDir, realDir, process.env.PATH)}, home: await freshDir(), timeoutMs: 300})
    assert.equal(result.command, real)
    assert.match(result.skipped[0].reason, /timed out/)
    assert.ok(Date.now() - started < 2000, 'returns at the deadline, not when the orphaned sleep ends')
  })

  it('records what an impostor printed so the user can recognise it', async () => {
    const [badDir, realDir] = [await freshDir(), await freshDir()]
    await putExecutable(badDir, 'echo "Electron 33.0.0"; exit 0')
    await putExecutable(realDir, REAL_CLI)
    const result = await resolveDevin({command: NAME, env: {PATH: pathOf(badDir, realDir)}, home: await freshDir()})
    assert.match(result.skipped[0].reason, /Electron 33\.0\.0/)
  })
})

describe('loadModelData with a shadowing launcher on PATH', () => {
  async function withPath(dirs, fn) {
    const saved = process.env.PATH
    process.env.PATH = pathOf(...dirs, saved)
    try {
      return await fn()
    } finally {
      process.env.PATH = saved
    }
  }

  it('runs the real CLI behind the launcher and reports what it skipped', async () => {
    const [shimDir, realDir] = [await freshDir(), await freshDir()]
    const shim = await putExecutable(shimDir, ELECTRON_SHIM)
    const real = await putExecutable(realDir, REAL_CLI)
    const loaded = await withPath([shimDir, realDir], () => loadModelData({command: NAME}))
    assert.equal(loaded.data.families.length, 1)
    assert.equal(loaded.retries, 0, 'no wasted retries against the wrong binary')
    assert.equal(loaded.resolution.command, real)
    assert.deepEqual(loaded.resolution.skipped.map((s) => s.path), [shim])
  })

  it('names the shadowing executable when no candidate is the CLI', async () => {
    const dir = await freshDir()
    const shim = await putExecutable(dir, ELECTRON_SHIM)
    await assert.rejects(
      () => withPath([dir], () => loadModelData({command: NAME})),
      (error) => {
        assert.match(error.message, /does not identify itself as the Devin CLI/)
        assert.ok(error.message.includes(shim), 'names the offending path')
        assert.match(error.message, /PATH/)
        assert.match(error.message, /alias/)
        assert.doesNotMatch(error.message, /click to show more/, 'not blamed on a pager')
        assert.match(error.message, /--source=-/)
        return true
      },
    )
  })

  it('does not blame PATH when the unverified command did write output', async () => {
    // A CLI whose --version banner changed still works (bare-name fallback);
    // if its capture then fails, the pager advice applies, not "wrong program".
    const dir = await freshDir()
    await putExecutable(dir, 'case "$1" in --version) echo "Devin Code CLI v9" ;; *) echo "click to show more" ;; esac')
    await assert.rejects(
      () => withPath([dir], () => loadModelData({command: NAME, timeoutMs: 2000})),
      (error) => {
        assert.doesNotMatch(error.message, /does not identify itself/)
        assert.match(error.message, /returned invalid JSON/)
        return true
      },
    )
  })

  it('still names the unverified candidates when the command fails with an error of its own', async () => {
    const dir = await freshDir()
    const shim = await putExecutable(dir, 'echo "Warning: not the CLI" >&2\nexit 1')
    await assert.rejects(
      () => withPath([dir], () => loadModelData({command: NAME})),
      (error) => {
        assert.match(error.message, /failed — Warning: not the CLI/)
        assert.ok(error.message.includes(shim), 'names the unverified path')
        return true
      },
    )
  })
})
