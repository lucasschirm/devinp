import ansis from 'ansis'

import {formatContext} from './models.js'

const HEAT = ['#22c55e', '#84cc16', '#eab308', '#f97316', '#ef4444']

// Color-free stand-in for ansis: chainable methods return the text unchanged,
// color-spec methods (hex/rgb/bg*) return a plain formatter.
const identity = (s) => s
const colorSpec = () => identity
const chainable = new Proxy(identity, {
  get: (_t, prop) => (/^(hex|rgb|bg)/i.test(prop) ? colorSpec : chainable),
})
const plain = new Proxy(
  {},
  {get: (_t, prop) => (/^(hex|rgb|bg)/i.test(prop) ? colorSpec : chainable)},
)

const pad = (text, width, align = 'left') => {
  const gap = width - ansis.strip(text).length
  const fill = ' '.repeat(Math.max(0, gap))
  return align === 'right' ? fill + text : text + fill
}

function formatPrice(c, value, min, max) {
  if (value == null) return c.dim('—')
  if (min === max) return c.hex(HEAT[0])(`$${value}`)
  const ratio = Math.min(1, Math.max(0, (value - min) / (max - min)))
  return c.hex(HEAT[Math.min(HEAT.length - 1, Math.floor(ratio * HEAT.length))])(`$${value}`)
}

function formatValue(c, row) {
  if (row.value == null) return c.dim('—')
  if (!Number.isFinite(row.value)) return c.magenta('∞')
  return c.bold.green(row.value.toFixed(2))
}

function formatSwe(c, row) {
  if (row.swe == null) return c.dim('—')
  const text = `${row.swe.resolved}%`
  return row.swe.resolved >= 70 ? c.cyan.bold(text) : c.cyan(text)
}

function formatLabel(c, row) {
  let text = row.free ? c.magenta(row.label) : c.bold(row.label)
  if (row.isBeta) text += c.dim(' β')
  if (row.isNew) text += c.green(' ●')
  return text
}

function formatSidekick(c, row) {
  if (!row.sidekick) return c.dim('—')
  return c.dim(`$${row.sidekick.input}/$${row.sidekick.cached}/$${row.sidekick.output}`)
}

function priceRange(rows, field) {
  const values = rows.map((r) => r[field]).filter((v) => v != null)
  return [Math.min(...values), Math.max(...values)]
}

export function colorsEnabled(stream = process.stdout) {
  if (process.env.FORCE_COLOR) return true
  if (process.env.NO_COLOR) return false
  return Boolean(stream.isTTY)
}

/**
 * Render sections [{header, rows}] as a colored unicode table.
 */
export function renderTable(sections, {sweMeta, colors = colorsEnabled()} = {}) {
  const c = colors ? ansis : plain
  const allRows = sections.flatMap((s) => s.rows)
  const hasSidekick = allRows.some((r) => r.sidekick)
  const ranges = {
    input: priceRange(allRows, 'input'),
    cached: priceRange(allRows, 'cached'),
    output: priceRange(allRows, 'output'),
  }

  const columns = [
    {title: '#', align: 'right'},
    {title: 'Model', align: 'left'},
    {title: 'Ctx', align: 'right'},
    {title: 'Input', align: 'right'},
    {title: 'Cached', align: 'right'},
    {title: 'Output', align: 'right'},
    {title: 'SWE %', align: 'right'},
    {title: 'Value', align: 'right'},
  ]
  if (hasSidekick) columns.push({title: 'Sidekick i/c/o', align: 'left'})

  let rank = 0
  const bodyRows = []
  for (const section of sections) {
    if (section.header) bodyRows.push({section: section.header})
    for (const row of section.rows) {
      rank += 1
      bodyRows.push({
        cells: [
          c.dim(`${rank}`),
          formatLabel(c, row),
          c.dim(formatContext(row.context)),
          formatPrice(c, row.input, ...ranges.input),
          formatPrice(c, row.cached, ...ranges.cached),
          formatPrice(c, row.output, ...ranges.output),
          formatSwe(c, row),
          formatValue(c, row),
          ...(hasSidekick ? [formatSidekick(c, row)] : []),
        ],
      })
    }
  }

  const widths = columns.map((col, i) => {
    const cellWidths = bodyRows
      .filter((r) => r.cells)
      .map((r) => ansis.strip(r.cells[i]).length)
    return Math.max(ansis.strip(col.title).length, ...cellWidths)
  })

  const border = (s) => c.hex('#475569')(s)
  const line = (left, mid, right) =>
    border(left + widths.map((w) => '─'.repeat(w + 2)).join(mid) + right)
  const renderRow = (cells) =>
    border('│') + cells.map((cell, i) => ` ${pad(cell, widths[i], columns[i].align)} ${border('│')}`).join('')
  const totalWidth = widths.reduce((a, w) => a + w + 3, 1)
  const sectionRow = (title) =>
    border('│') + ' ' + c.bold.hex('#7dd3fc')(pad(`▸ ${title}`, totalWidth - 3)) + ' ' + border('│')

  const out = [
    line('╭', '┬', '╮'),
    renderRow(columns.map((col) => c.bold.hex('#e2e8f0')(col.title))),
    line('├', '┼', '┤'),
  ]
  for (const row of bodyRows) {
    out.push(row.cells ? renderRow(row.cells) : sectionRow(row.section))
  }
  out.push(line('╰', '┴', '╯'))

  const legend = [
    c.dim('prices per 1M tokens'),
    c.dim('value = SWE % ÷ output $/1M'),
    sweMeta?.updated
      ? c.dim(`SWE-bench Verified (mini-SWE-agent) · updated ${sweMeta.updated}`)
      : c.dim('SWE scores: not available'),
  ]
  return `${out.join('\n')}\n${legend.join(c.dim(' · '))}`
}
