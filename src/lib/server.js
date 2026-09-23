import {execFile} from 'node:child_process'

import Fastify from 'fastify'

const PAGE = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>devinp — Devin model prices</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 32px 24px 48px;
    background: #0b1020; color: #e2e8f0;
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  }
  h1 { font-size: 20px; margin: 0 0 4px; }
  .sub { color: #64748b; font-size: 12px; margin-bottom: 20px; }
  .controls { display: flex; gap: 16px; align-items: center; margin-bottom: 16px; flex-wrap: wrap; }
  .controls label { color: #94a3b8; font-size: 12px; display: flex; gap: 6px; align-items: center; }
  select {
    background: #1e293b; color: #e2e8f0; border: 1px solid #334155;
    border-radius: 6px; padding: 4px 8px; font: inherit; font-size: 12px;
  }
  table { border-collapse: collapse; min-width: 720px; }
  th, td { padding: 6px 14px; text-align: right; white-space: nowrap; }
  th {
    color: #7dd3fc; font-size: 11px; text-transform: uppercase; letter-spacing: .08em;
    border-bottom: 1px solid #334155; position: sticky; top: 0; background: #0b1020;
  }
  td.name, th.name { text-align: left; font-weight: 600; }
  td.dim { color: #64748b; }
  td.free { color: #e879f9; }
  td.bench { color: #22d3ee; font-weight: 600; }
  td.value { color: #4ade80; font-weight: 700; }
  tr.section td {
    color: #7dd3fc; font-weight: 700; text-align: left;
    border-top: 1px solid #1e293b; padding-top: 14px;
  }
  .legend { margin-top: 18px; color: #64748b; font-size: 12px; }
</style>
</head>
<body>
  <h1>devinp</h1>
  <div class="sub">Devin model prices · per 1M tokens · value = Bench % ÷ output $/1M</div>
  <div class="controls">
    <label>sort
      <select id="order">
        <option value="value">best value (Bench ÷ output)</option>
        <option value="bench">BenchLM score</option>
        <option value="input">input price</option>
        <option value="cached">cached price</option>
        <option value="output">output price</option>
      </select>
    </label>
    <label>group
      <select id="group">
        <option value="">none</option>
        <option value="family">family</option>
        <option value="price">price</option>
        <option value="size">context size</option>
      </select>
    </label>
    <span class="sub" id="count"></span>
  </div>
  <table>
    <thead><tr>
      <th>#</th><th class="name">Model</th><th>Ctx</th><th>Input</th>
      <th>Cached</th><th>Output</th><th>Bench %</th><th>Value</th><th id="sk"></th>
    </tr></thead>
    <tbody id="body"></tbody>
  </table>
  <div class="legend" id="legend"></div>
<script>
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const ctx = (t) => t == null ? '—' : t >= 1e6 ? (+(t/1e6).toFixed(2)) + 'M' : t >= 1e3 ? Math.round(t/1e3) + 'K' : t;
const HEAT = ['#22c55e','#84cc16','#eab308','#f97316','#ef4444'];
const heat = (v, lo, hi) => v == null ? '<span class="dim">—</span>'
  : '<span style="color:' + HEAT[Math.min(4, Math.floor((hi === lo ? 0 : (v - lo) / (hi - lo)) * 5))] + '">$' + v + '</span>';

async function main() {
  const res = await fetch('/api/models');
  const {rows, bench} = await res.json();
  const range = (f) => { const vs = rows.map(r => r[f]).filter(v => v != null); return [Math.min(...vs), Math.max(...vs)]; };
  const RI = range('input'), RC = range('cached'), RO = range('output');
  const hasSk = rows.some(r => r.sidekick);
  if (hasSk) document.getElementById('sk').textContent = 'Sidekick i/c/o';

  const SORTS = {
    input: (a, b) => (a.input ?? 1e9) - (b.input ?? 1e9) || (a.output ?? 1e9) - (b.output ?? 1e9) || a.label.localeCompare(b.label),
    cached: (a, b) => (a.cached ?? 1e9) - (b.cached ?? 1e9) || (a.output ?? 1e9) - (b.output ?? 1e9) || a.label.localeCompare(b.label),
    output: (a, b) => (a.output ?? 1e9) - (b.output ?? 1e9) || a.label.localeCompare(b.label),
    bench: (a, b) => (b.bench?.score ?? -1) - (a.bench?.score ?? -1) || (a.output ?? 1e9) - (b.output ?? 1e9) || a.label.localeCompare(b.label),
    value: (a, b) => (b.value ?? -1) - (a.value ?? -1) || (b.bench?.score ?? -1) - (a.bench?.score ?? -1) || a.label.localeCompare(b.label),
  };
  const GROUPS = {
    family: {key: r => r.familySlug, head: r => r.familyLabel},
    price: {key: r => r.free ? 'free' : r.input + '/' + r.cached + '/' + r.output,
            head: r => r.free ? 'Free' : '$' + r.input + ' in · $' + r.cached + ' cached · $' + r.output + ' out / 1M'},
    size: {key: r => r.context ?? -1, head: r => ctx(r.context) + ' context'},
  };

  function render() {
    const order = document.getElementById('order').value;
    const group = document.getElementById('group').value;
    const sorted = [...rows].sort(SORTS[order]);
    const sections = [];
    if (group) {
      const g = GROUPS[group], map = new Map();
      for (const r of sorted) {
        const k = g.key(r);
        if (!map.has(k)) map.set(k, {head: g.head(r), rows: []});
        map.get(k).rows.push(r);
      }
      sections.push(...map.values());
    } else sections.push({head: null, rows: sorted});

    let html = '', n = 0;
    for (const s of sections) {
      if (s.head) html += '<tr class="section"><td colspan="9">▸ ' + esc(s.head) + '</td></tr>';
      for (const r of s.rows) {
        n++;
        const tags = (r.isBeta ? ' <span class="dim">β</span>' : '') + (r.isNew ? ' <span style="color:#4ade80">●</span>' : '');
        html += '<tr><td class="dim">' + n + '</td>'
          + '<td class="name' + (r.free ? ' free' : '') + '">' + esc(r.label) + tags + '</td>'
          + '<td class="dim">' + ctx(r.context) + '</td>'
          + '<td>' + heat(r.input, ...RI) + '</td>'
          + '<td>' + heat(r.cached, ...RC) + '</td>'
          + '<td>' + heat(r.output, ...RO) + '</td>'
          + '<td class="bench">' + (r.bench ? r.bench.score + '%' : '<span class="dim">—</span>') + '</td>'
          + '<td class="value">' + (r.value == null ? '<span class="dim">—</span>' : !isFinite(r.value) ? '∞' : r.value.toFixed(2)) + '</td>'
          + (hasSk ? '<td class="dim">' + (r.sidekick ? '$' + r.sidekick.input + '/$' + r.sidekick.cached + '/$' + r.sidekick.output : '—') + '</td>' : '')
          + '</tr>';
      }
    }
    document.getElementById('body').innerHTML = html;
    document.getElementById('count').textContent = n + ' rows';
    document.getElementById('legend').textContent = bench.updated
      ? 'BenchLM (coding) · scores updated ' + bench.updated
      : 'Bench scores not available';
  }
  document.getElementById('order').onchange = render;
  document.getElementById('group').onchange = render;
  render();
}
main();
</script>
</body>
</html>`

export async function startServer({rows, benchMeta, port, log}) {
  const app = Fastify({logger: false})

  app.get('/', async (_request, reply) => {
    reply.type('text/html').send(PAGE)
  })
  app.get('/api/models', async () => ({rows, bench: benchMeta ?? {}}))

  const address = await app.listen({port, host: '127.0.0.1'})
  log(`devinp web — ${address}`)

  // Best-effort browser open; silently ignored when unavailable.
  const opener = process.platform === 'darwin' ? 'open' : 'xdg-open'
  execFile(opener, [address], () => {})

  // Keep the process alive until Ctrl+C.
  await new Promise((resolve) => {
    process.on('SIGINT', async () => {
      await app.close()
      resolve()
    })
  })
}
