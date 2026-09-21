import {parseCostSummary} from './parse-cost.js'

// Thinking-effort words stripped from variant labels — every effort tier
// shares the same price, so they collapse into a single row.
const EFFORT_RE = /\b(x-?high|no thinking|thinking|none|minimal|low|medium|high|max)\b/gi

function normalizeLabel(label, fallback) {
  const stripped = label
    .replace(EFFORT_RE, '')
    .replace(/\s{2,}/g, ' ')
    .replace(/\(\s+/g, '(')
    .replace(/\s+\)/g, ')')
    .replace(/\s+([+])/g, ' $1')
    .trim()
  return stripped || fallback
}

export function formatContext(tokens) {
  if (tokens == null) return '—'
  if (tokens >= 1e6) return `${Number((tokens / 1e6).toFixed(2))}M`
  if (tokens >= 1e3) return `${Math.round(tokens / 1e3)}K`
  return `${tokens}`
}

function priceKey(cost) {
  return [cost.input, cost.cached, cost.output, cost.sidekick?.input, cost.sidekick?.cached, cost.sidekick?.output]
    .map((n) => (n == null ? '-' : n))
    .join('/')
}

/**
 * Flatten {families: [...]} into display rows. Effort variants that share the
 * same normalized label, context and prices collapse into one row.
 */
export function buildRows(data, sweScores = {}) {
  const rows = new Map()
  for (const family of data.families ?? []) {
    const swe = sweScores.models?.[family.slug] ?? null
    for (const variant of family.variants ?? []) {
      const cost = parseCostSummary(variant.cost_summary)
      const label = normalizeLabel(variant.label, family.family_label)
      const key = [family.slug, label, variant.max_context_tokens ?? '-', priceKey(cost)].join('|')
      const existing = rows.get(key)
      if (existing) {
        existing.variantCount += 1
        existing.isBeta ||= variant.is_beta
        existing.isNew ||= variant.is_new
        continue
      }
      rows.set(key, {
        label,
        familySlug: family.slug,
        familyLabel: family.family_label,
        context: variant.max_context_tokens ?? null,
        input: cost.input,
        cached: cost.cached,
        output: cost.output,
        sidekick: cost.sidekick,
        free: cost.free,
        isBeta: Boolean(variant.is_beta),
        isNew: Boolean(variant.is_new),
        variantCount: 1,
        swe: swe ? {resolved: swe.resolved, sweName: swe.sweName} : null,
        value: computeValue(swe?.resolved, cost.output),
      })
    }
  }
  return [...rows.values()]
}

function computeValue(resolved, output) {
  if (resolved == null) return null
  if (output == null) return null
  if (output === 0) return Number.POSITIVE_INFINITY
  return resolved / output
}

const priceAsc = (field) => (a, b) =>
  (a[field] ?? Number.POSITIVE_INFINITY) - (b[field] ?? Number.POSITIVE_INFINITY) ||
  (a.output ?? Number.POSITIVE_INFINITY) - (b.output ?? Number.POSITIVE_INFINITY) ||
  a.label.localeCompare(b.label)

export const SORTS = {
  input: priceAsc('input'),
  cached: priceAsc('cached'),
  output: priceAsc('output'),
  swe: (a, b) =>
    (b.swe?.resolved ?? -1) - (a.swe?.resolved ?? -1) ||
    (a.output ?? Number.POSITIVE_INFINITY) - (b.output ?? Number.POSITIVE_INFINITY) ||
    a.label.localeCompare(b.label),
  value: (a, b) =>
    (b.value ?? -1) - (a.value ?? -1) ||
    (b.swe?.resolved ?? -1) - (a.swe?.resolved ?? -1) ||
    a.label.localeCompare(b.label),
}

export const SORT_IDS = Object.keys(SORTS)

const GROUPS = {
  family: {
    key: (row) => row.familySlug,
    header: (row) => row.familyLabel,
  },
  price: {
    key: (row) => (row.free ? 'free' : `${row.input}/${row.cached}/${row.output}`),
    header: (row) =>
      row.free
        ? 'Free'
        : `$${row.input} in · $${row.cached} cached · $${row.output} out / 1M`,
  },
  size: {
    key: (row) => row.context ?? -1,
    header: (row) => `${formatContext(row.context)} context`,
  },
}

export const GROUP_IDS = Object.keys(GROUPS)

/**
 * Sort rows by `order`, optionally split into sections by `group`
 * (family | price | size). Sections are ordered by their best-ranked row.
 * Returns [{header: string|null, rows: [...]}].
 */
export function arrangeRows(rows, {order = 'value', group} = {}) {
  const compare = SORTS[order] ?? SORTS.value
  const sorted = [...rows].sort(compare)
  const groupDef = group ? GROUPS[group] : null
  if (!groupDef) return [{header: null, rows: sorted}]

  const sections = new Map()
  for (const row of sorted) {
    const key = groupDef.key(row)
    let section = sections.get(key)
    if (!section) {
      section = {header: groupDef.header(row), rows: []}
      sections.set(key, section)
    }
    section.rows.push(row)
  }
  return [...sections.values()]
}
