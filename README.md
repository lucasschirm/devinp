# devinp

Pretty pricing table for [Devin](https://devin.ai) models — reads live prices
from `devin models list`, joins SWE-bench Verified scores, and renders a
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
devinp                       # table sorted by best value (SWE % ÷ output price)
devinp -o=input              # cheapest input price first
devinp -o=cached             # cheapest cached-input price first
devinp -o=output             # cheapest output price first
devinp -o=swe                # highest SWE-bench Verified score first
devinp -o=value              # default: best SWE score per output dollar

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
│  # │ Model           │   Ctx │ Input │ Cached │ Output │  SWE % │ Value │ Sidekick i/c/o │
│  1 │ Gemini 3 Flash  │ 1.05M │  $0.5 │  $0.05 │     $3 │  75.8% │ 25.27 │ —              │
```

- Prices are USD per 1M tokens; columns are heat-colored green → red.
- **Value** = `SWE % ÷ output $/1M` — the score you buy per output dollar.
  Free models show `∞`; models without a SWE score show `—` and sort last
  under `-o=swe` / `-o=value`.
- Markers: `β` = beta, `●` = new, magenta name = free. `Sidekick` shows the
  partner-model pricing for Fusion rows.
- Colors disable automatically when piping; `NO_COLOR` / `FORCE_COLOR` are
  respected.

## SWE-bench scores

Scores live in `data/swe-scores.json` and ship with the package. To refresh
them (maintainers, manual — not automated):

```sh
npm run update-swe
```

This scrapes the embedded leaderboard JSON on
[swebench.com](https://www.swebench.com) — the **Verified** board's
`mini-SWE-agent` ("Bash Only") entries — and writes `data/swe-scores.json`.
Leaderboard names that don't map to a family slug are reported under
`unmatched`; add entries to `MANUAL_MAP` in `scripts/update-swe.js` to fix them.

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
