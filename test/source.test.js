import assert from 'node:assert/strict'
import {chmod, mkdir, readFile, readdir, writeFile} from 'node:fs/promises'
import {tmpdir} from 'node:os'
import {join} from 'node:path'
import {describe, it} from 'node:test'

import {loadModelData, runDevinJson} from '../src/lib/source.js'

const OK_DOC = JSON.stringify({families: [{slug: 'a', variants: []}]})

/**
 * Build an executable fake `devin` (a POSIX shell script) in a private
 * temp dir; `stdout`/`stderr` are written to data files the script cats.
 * `body` appends raw shell lines for the tricks (invocation counters,
 * environment dumps) and `before` lines run before any output.
 */
async function makeFakeDevin(name, {stdout = '', stderr = '', exitCode = 0, sleepSec = 0, before = [], body = []} = {}) {
  const dir = await mkdir(join(tmpdir(), `devinp-test-${process.pid}-${Date.now()}-${Math.random().toString(36).slice(2)}`), {recursive: true})
  if (stdout) await writeFile(join(dir, `${name}.out`), stdout)
  if (stderr) await writeFile(join(dir, `${name}.err`), stderr)
  const script = [
    '#!/bin/sh',
    ...before,
    sleepSec ? `sleep ${sleepSec}` : '',
    stdout ? 'cat "$0.out"' : '',
    stderr ? 'cat "$0.err" 1>&2' : '',
    ...body,
    `exit ${exitCode}`,
  ].filter(Boolean).join('\n')
  const file = join(dir, name)
  await writeFile(file, script)
  await chmod(file, 0o755)
  return file
}

/** Fake that stays silent (exit 0, no stdout) — the "pager ate it" case. */
const silentDevin = (name) => makeFakeDevin(name)

async function devinTempFiles() {
  const entries = await readdir(tmpdir())
  return new Set(entries.filter((e) => e.startsWith('devinp-models-')))
}

describe('runDevinJson', () => {
  it('captures the command output from the temp file', async () => {
    const fake = await makeFakeDevin('devin-ok.sh', {stdout: '{"families": []}'})
    const {raw, parsed, attempts} = await runDevinJson({command: fake})
    assert.equal(raw, '{"families": []}')
    assert.deepEqual(parsed, {families: []})
    assert.equal(attempts.length, 1)
    assert.equal(attempts[0].bytes, '{"families": []}'.length)
  })

  it('throws with the stderr detail when the command fails', async () => {
    const fake = await makeFakeDevin('devin-fail.sh', {stderr: 'stream disconnected\n', exitCode: 1})
    await assert.rejects(() => runDevinJson({command: fake}), /failed — stream disconnected/)
  })

  it('does not throw on a timeout — it reports the attempt instead', async () => {
    // sleep is long enough to outlive the 300ms timeout, short enough to
    // keep the suite fast when the orphaned process drains the event loop.
    const fake = await makeFakeDevin('devin-hang.sh', {sleepSec: 3})
    const {parsed, attempts} = await runDevinJson({command: fake, timeoutMs: 300})
    assert.equal(parsed, null)
    assert.ok(attempts.every((a) => a.timedOut), 'every attempt should be flagged as timed out')
  })

  it('escalates: retries with paging disabled and no controlling terminal', async () => {
    const fake = await makeFakeDevin('devin-flaky.sh', {
      stdout: OK_DOC,
      before: [
        'n=$(cat "$0.count" 2>/dev/null || echo 0)',
        'n=$((n+1)); echo "$n" > "$0.count"',
        `echo "run $n pid=$$ pgid=$(ps -o pgid= -p $$ | tr -d ' ') TERM=$TERM PAGER=$PAGER NO_COLOR=$NO_COLOR CI=$CI" >> "$0.log"`,
        '[ "$n" -lt 3 ] && exit 0',
      ],
    })
    const {parsed, attempts} = await runDevinJson({command: fake})
    assert.deepEqual(parsed, JSON.parse(OK_DOC))
    assert.equal(attempts.length, 3)
    assert.match(attempts[0].label, /^temp file$/)
    assert.match(attempts[1].label, /paging disabled/)
    // The third attempt captures over a pipe instead of a temp file.
    assert.match(attempts[2].label, /^pipe/)

    const lines = (await readFile(`${fake}.log`, 'utf8')).trim().split('\n')
    assert.equal(lines.length, 3)
    // Attempt 1 keeps the caller's environment; the retries say "not
    // interactive" so pagers, colours and prompts stay out of the stream.
    assert.doesNotMatch(lines[0], /TERM=dumb/)
    assert.match(lines[1], /TERM=dumb PAGER=cat NO_COLOR=1 CI=1/)
    assert.match(lines[2], /TERM=dumb PAGER=cat NO_COLOR=1 CI=1/)
    // A detached spawn leads its own session, so it has no controlling
    // terminal to paint a paged view on (pgid === pid).
    for (const [i, line] of lines.entries()) {
      const pid = Number(/pid=(\d+)/.exec(line)[1])
      const pgid = Number(/pgid=(\d+)/.exec(line)[1])
      if (i === 0) assert.notEqual(pgid, pid, 'attempt 1 stays in devinp\'s process group')
      else assert.equal(pgid, pid, `attempt ${i + 1} must lead its own session`)
    }
  })

  it('salvages JSON that arrives coloured or wrapped in chatter', async () => {
    const noisy = `an update is available — run \`devin update\`\n\u001b[32m${OK_DOC}\u001b[39m\npass a slug to --model\n`
    const fake = await makeFakeDevin('devin-noisy.sh', {stdout: noisy})
    const {parsed} = await runDevinJson({command: fake})
    assert.deepEqual(parsed, JSON.parse(OK_DOC))
  })

  it('removes its temp file on success and on failure', async () => {
    const before = await devinTempFiles()
    const ok = await makeFakeDevin('devin-c1.sh', {stdout: '{}'})
    await runDevinJson({command: ok})
    const bad = await makeFakeDevin('devin-c2.sh', {exitCode: 3})
    const outcome = await runDevinJson({command: bad})
    assert.equal(outcome.parsed, null)
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
    const {data, truncated, retries} = await loadModelData({command: fake})
    assert.equal(truncated, undefined)
    assert.equal(retries, 0)
    assert.equal(data.families.length, 1)
  })

  it('beats a devin that keeps its paged view as long as it has a terminal', async () => {
    // The reported failure: the CLI draws its interactive "click to show
    // more" list and writes 0 bytes. Such a CLI is simulated here by a
    // fake that only answers once it has no controlling terminal — which
    // is exactly what the detached retries give it.
    const fake = await makeFakeDevin('devin-pager.sh', {
      stdout: JSON.stringify({families: [{slug: 'a', variants: []}]}),
      before: [`[ "$(ps -o pgid= -p $$ | tr -d ' ')" != "$$" ] && exit 0`],
    })
    const {data, retries} = await loadModelData({command: fake})
    assert.equal(data.families.length, 1)
    assert.equal(retries, 1)
  })

  it('reports how many extra attempts the machine needed', async () => {
    const fake = await makeFakeDevin('devin-twice-silent.sh', {
      stdout: JSON.stringify({families: [{slug: 'a', variants: []}]}),
      before: ['n=$(cat "$0.count" 2>/dev/null || echo 0)', 'n=$((n+1)); echo "$n" > "$0.count"', '[ "$n" -lt 3 ] && exit 0'],
    })
    const {data, retries} = await loadModelData({command: fake})
    assert.equal(data.families.length, 1)
    assert.equal(retries, 2)
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

  it('explains a silent devin (exit 0, nothing on stdout) and points at --source', async () => {
    const fake = await silentDevin('devin-silent.sh')
    await assert.rejects(
      () => loadModelData({command: fake}),
      (error) => {
        assert.match(error.message, /exited without writing the JSON to stdout \(0 bytes\)/)
        assert.match(error.message, /click to show more/)
        assert.match(error.message, /Tried: temp file: 0 bytes, exit 0/)
        assert.match(error.message, /devinp --source=-/)
        return true
      },
    )
  })

  it('reports the timeout in the failure diagnostics', async () => {
    const fake = await makeFakeDevin('devin-live-hang.sh', {sleepSec: 3})
    await assert.rejects(() => loadModelData({command: fake, timeoutMs: 300}), /timed out after 0\.3s/)
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

  it('reads the snapshot from stdin with source "-"', async () => {
    const {data} = await loadModelData({source: '-', readStdinFrom: async () => JSON.stringify({families: [{slug: 'a'}]})})
    assert.equal(data.families.length, 1)
  })

  it('names stdin in the error when the piped document is broken', async () => {
    await assert.rejects(
      () => loadModelData({source: '-', readStdinFrom: async () => '{"families": [{"slug": "a"}'}),
      /stdin is not valid JSON/,
    )
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
