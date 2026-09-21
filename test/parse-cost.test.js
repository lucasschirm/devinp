import assert from 'node:assert/strict'
import {describe, it} from 'node:test'

import {parseCostSummary} from '../src/lib/parse-cost.js'

describe('parseCostSummary', () => {
  it('parses a standard input/cached/output triple', () => {
    const cost = parseCostSummary('$5 / 1M Input · $0.5 / 1M Cached input · $25 / 1M Output')
    assert.deepEqual(cost, {free: false, input: 5, cached: 0.5, output: 25, sidekick: null})
  })

  it('parses sidekick segments', () => {
    const cost = parseCostSummary(
      '$10 / 1M Input · $0.25 / 1M Cached input · $50 / 1M Output · $0.2 / 1M Sidekick input · $0.02 / 1M Sidekick cached input · $1.2 / 1M Sidekick output',
    )
    assert.equal(cost.input, 10)
    assert.equal(cost.output, 50)
    assert.deepEqual(cost.sidekick, {input: 0.2, cached: 0.02, output: 1.2})
  })

  it('treats a missing summary as free', () => {
    const cost = parseCostSummary(undefined)
    assert.equal(cost.free, true)
    assert.equal(cost.output, 0)
  })

  it('parses decimal prices', () => {
    const cost = parseCostSummary('$0.14 / 1M Input · $0.03 / 1M Cached input · $0.28 / 1M Output')
    assert.equal(cost.input, 0.14)
    assert.equal(cost.cached, 0.03)
    assert.equal(cost.output, 0.28)
  })
})
