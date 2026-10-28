import assert from 'node:assert/strict'
import {describe, it} from 'node:test'

import {checkTarball, extractEntry, parsePackOutput} from '../scripts/check-pack.js'

const goodPaths = [
  'LICENSE',
  'README.md',
  'bin/command.js',
  'bin/run.js',
  'data/benchlm-scores.json',
  'package.json',
]
const goodDisk = {
  binFiles: ['command.js', 'run.js'],
  dataFiles: ['benchlm-scores.json'],
}

describe('checkTarball — happy path', () => {
  it('passes on the expected tarball', () => {
    assert.deepEqual(checkTarball(goodPaths, goodDisk), [])
  })
})

describe('checkTarball — bench/ exclusion', () => {
  it('fails when bench/ entries are in the tarball', () => {
    const failures = checkTarball(
      [...goodPaths, 'bench/runner.py', 'bench'],
      goodDisk,
    )
    assert.ok(failures.some((f) => f.includes('tarball contains bench/ entries')))
    assert.ok(failures.some((f) => f.includes('bench/runner.py')))
  })

  it('fails for nested /bench/ paths, not just a top-level bench/ dir', () => {
    const failures = checkTarball([...goodPaths, 'x/bench/y.json'], goodDisk)
    assert.ok(failures.some((f) => f.includes('x/bench/y.json')))
  })
})

describe('checkTarball — completeness (the 1deccb0 vacuous-pass regression)', () => {
  it('fails when "bin" is dropped from files: run.js still ships via the bin field, command.js is lost', () => {
    const paths = goodPaths.filter((p) => p !== 'bin/command.js')
    const failures = checkTarball(paths, goodDisk)
    assert.ok(
      failures.some((f) => f === 'bin/ files missing from tarball: command.js'),
    )
  })

  it('fails when data/ files are missing', () => {
    const paths = goodPaths.filter((p) => p !== 'data/benchlm-scores.json')
    const failures = checkTarball(paths, goodDisk)
    assert.ok(
      failures.some((f) => f === 'data/ files missing from tarball: benchlm-scores.json'),
    )
  })

  it('fails when bin/ is empty on disk after the build (no vacuous pass)', () => {
    const failures = checkTarball(goodPaths, {...goodDisk, binFiles: []})
    assert.ok(failures.some((f) => f === 'bin/ is empty on disk after the build'))
  })

  it('fails when data/ is empty on disk after the build', () => {
    const failures = checkTarball(goodPaths, {...goodDisk, dataFiles: []})
    assert.ok(failures.some((f) => f === 'data/ is empty on disk after the build'))
  })

  it('fails when package.json is missing from the tarball', () => {
    const paths = goodPaths.filter((p) => p !== 'package.json')
    const failures = checkTarball(paths, goodDisk)
    assert.ok(failures.some((f) => f === 'tarball missing package.json'))
  })
})

describe('checkTarball — allowlist (nothing extra ships)', () => {
  it('fails when other repo directories are shipped (files regression)', () => {
    const failures = checkTarball(
      [...goodPaths, 'src/lib/models.js', 'test/models.test.js', 'sample/models.json'],
      goodDisk,
    )
    const unexpected = failures.find((f) => f.startsWith('unexpected tarball entries:'))
    assert.ok(unexpected)
    assert.ok(unexpected.includes('src/lib/models.js'))
    assert.ok(unexpected.includes('test/models.test.js'))
    assert.ok(unexpected.includes('sample/models.json'))
  })

  it('does not flag expected top-level files or bin//data/ contents', () => {
    assert.deepEqual(checkTarball(goodPaths, goodDisk), [])
  })
})

describe('checkTarball — npm always-ignored files on disk', () => {
  it('does not require .DS_Store, .npmrc, *.orig, package-lock.json, npm-debug.log in the tarball', () => {
    const failures = checkTarball(goodPaths, {
      binFiles: ['command.js', 'run.js', '.DS_Store'],
      dataFiles: ['benchlm-scores.json', '.npmrc', 'scores.orig', 'package-lock.json', 'npm-debug.log'],
    })
    assert.deepEqual(failures, [])
  })
})

describe('extractEntry — npm pack --json shape drift', () => {
  const entry = {id: '@lucasschirm/devinp@0.1.10', files: [{path: 'package.json'}]}

  it('npm 11 shape: single-element array', () => {
    assert.deepEqual(extractEntry([entry]), entry)
  })

  it('npm 12 shape: object keyed by package name', () => {
    assert.deepEqual(extractEntry({'@lucasschirm/devinp': entry}), entry)
  })

  it('plain object shape', () => {
    assert.deepEqual(extractEntry(entry), entry)
  })

  it('no file list anywhere: returns the input so the guards fail with diagnostics', () => {
    assert.deepEqual(extractEntry({id: 'x', files: undefined}), {id: 'x', files: undefined})
    assert.deepEqual(extractEntry([{}]), {})
  })
})

describe('parsePackOutput — CLI output variants', () => {
  const arrayShape = JSON.stringify([{files: [{path: 'bin/run.js'}]}])
  const keyedShape = JSON.stringify({'@lucasschirm/devinp': {files: [{path: 'bin/run.js'}]}})

  it('parses the npm 11 array shape', () => {
    assert.deepEqual(parsePackOutput(arrayShape).files, [{path: 'bin/run.js'}])
  })

  it('parses the npm 12 keyed shape', () => {
    assert.deepEqual(parsePackOutput(keyedShape).files, [{path: 'bin/run.js'}])
  })

  it('recovers from leading non-JSON stdout noise', () => {
    const noisy = 'built bin/run.js + bin/command.js\n' + arrayShape
    assert.deepEqual(parsePackOutput(noisy).files, [{path: 'bin/run.js'}])
  })

  it('returns null (→ FAIL in main) when no JSON block exists', () => {
    assert.equal(parsePackOutput('no json here at all'), null)
  })
})
