import assert from 'node:assert/strict'
import {describe, it} from 'node:test'

import {recoverTruncatedJson} from '../src/lib/repair-json.js'

describe('recoverTruncatedJson', () => {
  it('returns valid JSON unchanged', () => {
    assert.deepEqual(recoverTruncatedJson('{"a": [1, 2]}'), {a: [1, 2]})
  })

  it('returns null for non-string input', () => {
    assert.equal(recoverTruncatedJson(undefined), null)
    assert.equal(recoverTruncatedJson(null), null)
    assert.equal(recoverTruncatedJson(42), null)
  })

  it('closes a document truncated right after the last complete element', () => {
    const full = JSON.stringify({families: [{slug: 'a'}, {slug: 'b'}]})
    const doc = full.slice(0, -1) // drop the final '}'
    assert.deepEqual(recoverTruncatedJson(doc), {families: [{slug: 'a'}, {slug: 'b'}]})
  })

  it('drops a trailing partial object (cut mid-string)', () => {
    const doc = '{"families": [{"slug": "a"}, {"slug": "b"}, {"family_label": "Clau'
    const out = recoverTruncatedJson(doc)
    assert.ok(out)
    assert.deepEqual(out.families, [{slug: 'a'}, {slug: 'b'}])
  })

  it('drops a trailing partial object (cut after an inner comma)', () => {
    const doc = '{"families": [{"slug": "a"}, {"slug": "b", "cost_tier": "High'
    const out = recoverTruncatedJson(doc)
    assert.ok(out)
    assert.deepEqual(out.families, [{slug: 'a'}, {slug: 'b'}])
  })

  it('keeps a trailing complete value (a partially streamed number stays a value)', () => {
    assert.deepEqual(recoverTruncatedJson('{"a": 12'), {a: 12})
  })

  it('closes an array truncated mid-string', () => {
    assert.deepEqual(recoverTruncatedJson('["abc", "de'), ['abc'])
  })

  it('returns null when nothing salvageable remains', () => {
    assert.equal(recoverTruncatedJson(''), null)
    assert.equal(recoverTruncatedJson('{"a":'), null)
    assert.equal(recoverTruncatedJson('{"a": tru'), null)
    assert.equal(recoverTruncatedJson('not json at all'), null)
  })

  it('recovers a large pretty-printed document cut mid-way (the real-world shape)', () => {
    const full = JSON.stringify(
      {
        families: Array.from({length: 100}, (_, i) => ({
          family_label: `Fam ${i}`,
          slug: `fam-${i}`,
          variants: [{label: `Fam ${i}`, cost_summary: '$1 / 1M Input · $2 / 1M Output'}],
        })),
      },
      null,
      2,
    )
    const doc = full.slice(0, Math.floor(full.length * 0.6))
    const out = recoverTruncatedJson(doc)
    assert.ok(out)
    assert.ok(out.families.length >= 55 && out.families.length <= 65)
    assert.equal(out.families[0].slug, 'fam-0')
  })
})
