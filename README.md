# devinp

Pretty pricing table for [Devin](https://devin.ai) models — reads live prices
from `devin models list`, joins BenchLM coding scores, and renders a
sorted table in your terminal or a web page.

Thinking-effort variants that share a price (`low`, `medium`, `high`, `xhigh`,
`max`, …) are collapsed into one row, so you see one row per price tier — e.g.
`Claude Opus 5` and `Claude Opus 5 Fast` instead of ten near-identical rows.

## Install

```sh
npm i -g @lucasschirm/devinp
```

The package ships as a self-contained bundle — no dependencies are installed.

### Requirements

- Node.js ≥ 18
- The [`devin` CLI](https://devin.ai) installed and signed in — prices come from
  your account via `devin models list --format json`. If `devin` is missing or
  not authenticated, `devinp` exits with an error (use `--source` for a saved
  snapshot).

## Usage

```sh
devinp                       # table sorted by best value (Bench % ÷ output price)
devinp -o=input              # cheapest input price first
devinp -o=cached             # cheapest cached-input price first
devinp -o=output             # cheapest output price first
devinp -o=bench              # highest BenchLM coding score first
devinp -o=value              # default: best Bench score per output dollar

devinp -g=family             # section the table by model family
devinp -g=price              # section by identical price tier
devinp -g=size               # section by context window size

devinp --web                 # serve an interactive page on http://127.0.0.1:4321
devinp --web --port 8080     # pick a port

devinp --source models.json  # read a saved `devin models list --format json`
devinp --json                # emit normalized rows as JSON
```

### Reading the table

```
│  # │ Model           │   Ctx │ Input │ Cached │ Output │ Bench % │ Value │ Sidekick i/c/o │
│  1 │ Gemini 3.8 Flash│ 1.05M │ $0.75 │  $0.08 │  $3.75 │  64.1%  │ 17.10 │ —              │
```

- Prices are USD per 1M tokens; columns are heat-colored green → red.
- **Value** = `Bench % ÷ output $/1M` — the score you buy per output dollar.
  Free models always show `∞` and sort first under `-o=bench` / `-o=value`,
  even without a Bench score. Paid models without a score show `—` and sort
  last.
- Markers: `β` = beta, `●` = new, magenta name = free. `Sidekick` shows the
  partner-model pricing for Fusion rows.
- Colors disable automatically when piping; `NO_COLOR` / `FORCE_COLOR` are
  respected.

## BenchLM scores

Scores live in `data/benchlm-scores.json` and ship with the package. To refresh
them (maintainers, manual — not automated):

```sh
npm run update-benchlm
```

This fetches the [BenchLM](https://benchlm.ai/embed) leaderboard API
(`api/data/leaderboard?category=coding&limit=200` — the limit is raised from
the default 50 so the long tail of the board is included) and writes
`data/benchlm-scores.json`. Model names are
matched to Devin family slugs order-insensitively; names that still don't map
are reported under `unmatched` — add entries to `MANUAL_MAP` in
`scripts/update-benchlm.js` to fix them.

## Development

```sh
npm install     # installs dev deps (esbuild, oclif, fastify, ansis)
npm run build   # bundle src/ → bin/run.js + bin/command.js (self-contained)
npm run dev     # build + run
npm test        # node --test against sample/models.json
```

`bin/` is generated and gitignored; `prepare`/`prepack` build it automatically
so `npm publish`, git-URL installs, and `npm link` always ship a fresh bundle.

## Publishing

`.github/workflows/publish.yml` bumps the patch version and publishes with
[npm trusted publishing](https://docs.npmjs.com/trusted-publishers) (OIDC —
no `NPM_TOKEN`). Requires the trusted publisher to be configured on npmjs.com:
repository `lucasschirm/devinp`, workflow `publish.yml`.

## License

MIT
