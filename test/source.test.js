import assert from 'node:assert/strict'
import {chmod, mkdir, readdir, writeFile} from 'node:fs/promises'
import {tmpdir} from 'node:os'
import {join} from 'node:path'
import {describe, it} from 'node:test'

import {loadModelData, runDevinJson} from '../src/lib/source.js'

/**
 * Build an executable fake `devin` (a POSIX shell script) in a private
 * temp dir; `stdout`/`stderr` are written to data files the script cats.
 */
async function makeFakeDevin(name, {stdout = '', stderr = '', exitCode = 0, sleepSec = 0}) {
  const dir = await mkdir(join(tmpdir(), `devinp-test-${process.pid}-${Date.now()}-${Math.random().toString(36).slice(2)}`), {recursive: true})
  if (stdout) await writeFile(join(dir, `${name}.out`), stdout)
  if (stderr) await writeFile(join(dir, `${name}.err`), stderr)
  const script = [
    '#!/bin/sh',
    sleepSec ? `sleep ${sleepSec}` : '',
    stdout ? 'cat "$0.out"' : '',
    stderr ? 'cat "$0.err" 1>&2' : '',
    `exit ${exitCode}`,
  ].filter(Boolean).join('\n')
  const file = join(dir, name)
  await writeFile(file, script)
  await chmod(file, 0o755)
  return file
}

async function devinTempFiles() {
  const entries = await readdir(tmpdir())
  return new Set(entries.filter((e) => e.startsWith('devinp-models-')))
}

describe('runDevinJson', () => {
  it('captures the command output from the temp file', async () => {
    const fake = await makeFakeDevin('devin-ok.sh', {stdout: '{"families": []}'})
    const raw = await runDevinJson({command: fake})
    assert.equal(raw, '{"families": []}')
  })

  it('throws with the stderr detail when the command fails', async () => {
    const fake = await makeFakeDevin('devin-fail.sh', {stderr: 'stream disconnected\n', exitCode: 1})
    await assert.rejects(() => runDevinJson({command: fake}), /failed — stream disconnected/)
  })

  it('throws a timeout error and kills a hanging command', async () => {
    // sleep is long enough to outlive the 300ms timeout, short enough to
    // keep the suite fast when the orphaned process drains the event loop.
    const fake = await makeFakeDevin('devin-hang.sh', {sleepSec: 3})
    await assert.rejects(() => runDevinJson({command: fake, timeoutMs: 300}), /timed out after 0.3s/)
  })

  it('removes its temp file on success and on failure', async () => {
    const before = await devinTempFiles()
    const ok = await makeFakeDevin('devin-c1.sh', {stdout: '{}'})
    await runDevinJson({command: ok})
    const bad = await makeFakeDevin('devin-c2.sh', {exitCode: 3})
    await assert.rejects(() => runDevinJson({command: bad}))
    const after = await devinTempFiles()
    for (const file of after) {
      assert.ok(before.has(file), `leftover temp file: ${file}`)
    }
  })
})

describe('loadModelData (live path)', () => {
  it('returns data when devin output is complete', async () => {
    const fake = await makeFakeDevin('devin-live-ok.sh', {
      stdout: JSON.stringify({families: [{slug: 'a', variants: []}]}),
    })
    const {data, truncated} = await loadModelData({command: fake})
    assert.equal(truncated, undefined)
    assert.equal(data.families.length, 1)
  })

  it('recovers a document truncated mid-stream and flags it', async () => {
    const doc = JSON.stringify(
      {
        families: [
          {family_label: 'Alpha', slug: 'alpha', variants: [{label: 'Alpha'}]},
          {family_label: 'Beta', slug: 'beta', variants: [{label: 'Beta'}]},
          {family_label: 'Claude', slug: 'claude', variants: [{label: 'Claude'}]},
        ],
      },
      null,
      2,
    )
    const truncated = doc.slice(0, doc.indexOf('"Claude"') + 5) // cut mid-string
    const fake = await makeFakeDevin('devin-live-trunc.sh', {stdout: truncated})
    const {data, truncated: wasTruncated} = await loadModelData({command: fake})
    assert.equal(wasTruncated, true)
    assert.deepEqual(
      data.families.map((f) => f.slug),
      ['alpha', 'beta'],
    )
  })

  it('reports an unrepairable document with size and output tail', async () => {
    const fake = await makeFakeDevin('devin-live-garbage.sh', {stdout: 'totally not json'})
    await assert.rejects(
      () => loadModelData({command: fake}),
      (error) => {
        assert.match(error.message, /returned invalid JSON/)
        assert.match(error.message, /16 bytes/)
        assert.match(error.message, /totally not json/)
        assert.match(error.message, /--source models\.json/)
        return true
      },
    )
  })

  it('throws a friendly error when devin is not installed', async () => {
    await assert.rejects(
      () => loadModelData({command: '/nonexistent/devin-not-installed'}),
      /`devin` CLI not found in PATH/,
    )
  })

  it('rejects valid JSON without a families array', async () => {
    const fake = await makeFakeDevin('devin-live-nofam.sh', {stdout: '{"models": []}'})
    await assert.rejects(() => loadModelData({command: fake}), /has no "families" array/)
  })

  it('rejects an empty families array instead of succeeding', async () => {
    const fake = await makeFakeDevin('devin-live-empty.sh', {stdout: '{"families": []}'})
    await assert.rejects(() => loadModelData({command: fake}), /contains no model families/)
  })
})

describe('loadModelData (--source path)', () => {
  async function writeSource(name, content) {
    const file = join(tmpdir(), `devinp-test-${process.pid}-${Date.now()}-${name}`)
    await writeFile(file, content)
    return file
  }

  it('reads a saved snapshot', async () => {
    const file = await writeSource('ok.json', JSON.stringify({families: [{slug: 'a', variants: []}]}))
    const {data, truncated} = await loadModelData({source: file})
    assert.equal(truncated, undefined)
    assert.equal(data.families.length, 1)
  })

  it('fails a truncated snapshot with a clear message (no repair for --source)', async () => {
    const file = await writeSource('trunc.json', '{"families": [{"slug": "a"}')
    await assert.rejects(
      () => loadModelData({source: file}),
      (error) => {
        assert.match(error.message, /is not valid JSON/)
        return true
      },
    )
  })

  it('fails an empty families array', async () => {
    const file = await writeSource('empty.json', '{"families": []}')
    await assert.rejects(() => loadModelData({source: file}), /contains no model families/)
  })

  it('fails a missing file', async () => {
    await assert.rejects(() => loadModelData({source: '/nonexistent/models.json'}), /cannot read source file/)
  })
})
