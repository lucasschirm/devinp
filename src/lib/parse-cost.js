const SEGMENT_RE = /^\$(\d+(?:\.\d+)?) \/ 1M (.+)$/

const LABELS = {
  input: 'input',
  'cached input': 'cached',
  output: 'output',
  'sidekick input': 'sidekick.input',
  'sidekick cached input': 'sidekick.cached',
  'sidekick output': 'sidekick.output',
}

/**
 * Parse a `cost_summary` string like
 * "$5 / 1M Input · $0.5 / 1M Cached input · $25 / 1M Output"
 * (optionally followed by Sidekick segments) into structured prices.
 *
 * Returns {free, input, cached, output, sidekick?} — prices in USD per 1M tokens.
 * A missing summary means the model is free.
 */
export function parseCostSummary(summary) {
  if (!summary) return {free: true, input: 0, cached: 0, output: 0, sidekick: null}

  const cost = {free: false, input: null, cached: null, output: null, sidekick: null}
  for (const segment of summary.split('·')) {
    const match = SEGMENT_RE.exec(segment.trim())
    if (!match) continue
    const amount = Number.parseFloat(match[1])
    const key = LABELS[match[2].toLowerCase()]
    if (!key) continue
    if (key.startsWith('sidekick.')) {
      cost.sidekick ??= {}
      cost.sidekick[key.slice('sidekick.'.length)] = amount
    } else {
      cost[key] = amount
    }
  }
  return cost
}
