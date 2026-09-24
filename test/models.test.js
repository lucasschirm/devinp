import assert from 'node:assert/strict'
import {readFileSync} from 'node:fs'
import {fileURLToPath} from 'node:url'
import {describe, it} from 'node:test'

import {arrangeRows, buildRows, formatContext} from '../src/lib/models.js'

const data = JSON.parse(
  readFileSync(fileURLToPath(new URL('../sample/models.json', import.meta.url)), 'utf8'),
)
const rows = buildRows(data, {models: {}})

describe('buildRows', () => {
  it('collapses effort variants sharing a price', () => {
    const opus5 = rows.filter((r) => r.familySlug === 'claude-opus-5')
    assert.equal(opus5.length, 2) // standard + fast tiers
    assert.ok(opus5.some((r) => r.label === 'Claude Opus 5' && r.input === 5))
    assert.ok(opus5.some((r) => r.label === 'Claude Opus 5 Fast' && r.input === 10))
    assert.equal(opus5.find((r) => r.input === 5).variantCount, 5)
  })

  it('marks free models', () => {
    const swe2 = rows.filter((r) => r.familySlug === 'swe-2')
    assert.equal(swe2.length, 1)
    assert.equal(swe2[0].free, true)
    assert.equal(swe2[0].output, 0)
  })

  it('keeps rows apart when context differs', () => {
    const glm = rows.filter((r) => r.familySlug === 'glm-5.2')
    assert.equal(glm.length, 2) // 200K and 1M variants
    assert.deepEqual(
      glm.map((r) => r.context).sort((a, b) => a - b),
      [200_000, 1_000_000],
    )
  })

  it('strips effort words including "No Thinking" and "X-High"', () => {
    const inkling = rows.filter((r) => r.familySlug === 'inkling')
    assert.equal(inkling.length, 1)
    assert.equal(inkling[0].label, 'Inkling')
    const luna = rows.filter((r) => r.familySlug === 'gpt-5.6-luna')
    assert.ok(luna.some((r) => r.label === 'GPT-5.6 Luna'))
    assert.ok(luna.some((r) => r.label === 'GPT-5.6 Luna Fast'))
  })

  it('parses sidekick pricing on fusion rows', () => {
    const fusion = rows.filter((r) => r.familySlug === 'fusion')
    assert.ok(fusion.length > 0)
    const withSk = fusion.filter((r) => r.sidekick)
    assert.ok(withSk.length > 0)
    assert.ok(withSk.every((r) => r.label.startsWith('Fusion (')))
  })

  it('computes value = score / output and leaves it null without a score', () => {
    const scored = buildRows(data, {models: {'gemini-3-flash': {score: 75.8}}})
    const flash = scored.find((r) => r.familySlug === 'gemini-3-flash')
    assert.ok(Math.abs(flash.value - 75.8 / flash.output) < 1e-9)
    const unscored = scored.find((r) => r.familySlug === 'claude-opus-5')
    assert.equal(unscored.value, null)
  })
})

describe('arrangeRows', () => {
  it('returns one flat section when no group is given', () => {
    const sections = arrangeRows(rows, {order: 'input'})
    assert.equal(sections.length, 1)
    assert.equal(sections[0].header, null)
    const inputs = sections[0].rows.map((r) => r.input ?? Number.POSITIVE_INFINITY)
    assert.deepEqual(inputs, [...inputs].sort((a, b) => a - b))
  })

  it('groups by family', () => {
    const sections = arrangeRows(rows, {order: 'value', group: 'family'})
    assert.equal(sections.length, 48)
    assert.ok(sections.every((s) => typeof s.header === 'string'))
  })

  it('puts paid rows without Bench scores last in bench order', () => {
    const sections = arrangeRows(rows, {order: 'bench'})
    const flat = sections[0].rows.filter((r) => !r.free)
    const scored = flat.filter((r) => r.bench)
    assert.ok(scored.length === 0 || flat.indexOf(scored.at(-1)) < flat.indexOf(flat.find((r) => !r.bench) ?? flat.at(-1)))
  })

  it('puts free models first even without a bench score', () => {
    for (const order of ['bench', 'value']) {
      const flat = arrangeRows(rows, {order})[0].rows
      const lastFree = flat.reduce((i, r, j) => (r.free ? j : i), -1)
      const firstPaid = flat.findIndex((r) => !r.free)
      assert.ok(lastFree === -1 || firstPaid === -1 || lastFree < firstPaid)
      assert.equal(flat[0].free, true)
    }
    const freeRow = rows.find((r) => r.free)
    assert.equal(freeRow.value, Number.POSITIVE_INFINITY)
  })
})

describe('formatContext', () => {
  it('formats token counts', () => {
    assert.equal(formatContext(1_000_000), '1M')
    assert.equal(formatContext(262_000), '262K')
    assert.equal(formatContext(null), '—')
  })
})
