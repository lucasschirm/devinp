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

### When `devin` fails, pages, or truncates

`devin` is an interactive CLI, and on some machines it stays interactive even
when a script calls it: the model list comes back paged ("click to show more")
and the JSON never reaches stdout, so `devinp` used to die with *"returned
invalid JSON (… 0 bytes)"*. It now works harder to get the whole document:

- **A pipe first, so `TMPDIR` cannot break anything.** The happy path captures
  `devin`'s stdout over a pipe — no scratch file, no permissions, no sandbox.
  Measured against the real CLI, the piped document is byte-identical to the
  file capture (471 855 bytes, 46 families), so the old "pipes truncate" worry
  that put a temp file first was misplaced; an unwritable or missing `TMPDIR`
  used to cost two extra `devin` runs, and a missing one even reported *"`devin`
  CLI not found in PATH"*.
- **Retries that take the terminal away from `devin`.** If an attempt yields no
  usable JSON, `devinp` runs `devin` again, each time stripping more of the
  interactive terminal off it:

  | # | stdout goes to | environment | controlling terminal |
  | - | ------------ | -------------------------------------------------------- | --------------------- |
  | 1 | pipe | yours, untouched | yes — the healthy path, one `devin` call |
  | 2 | pipe | `PAGER=cat`, `GIT_PAGER=cat`, `TERM=dumb`, `NO_COLOR=1`, `CI=1` | **none** |
  | 3 | scratch file | same | **none** |

  Spawned in its own session, `devin` cannot open `/dev/tty`: a pager or a TUI
  that paints a "click to show more" view onto the terminal has nowhere to
  draw, so the document has to come out of stdout. Nothing lands on the screen
  you are typing in, and Ctrl-C there no longer kills the lookup. The first
  attempt that parses wins, so the healthy path is unchanged.
- **The scratch file (attempt 3) walks candidate directories** — `DEVINP_TMP`,
  `os.tmpdir()`, `~/.devinp`, then the current directory — and uses the first
  one it can actually write, removing the file afterwards. Set `DEVINP_TMP` to
  pin it somewhere you control.
- Noise around the document is tolerated: ANSI colours, an update banner in
  front, or the `pass a slug to --model` hint behind are stripped before the
  JSON is parsed.
- If `devin`'s output is cut off mid-stream, `devinp` repairs the document,
  prints a warning, and renders the partial table.
- A real CLI failure (not signed in, network down) is reported as-is instead of
  being retried.
- If nothing worked, the error lists all three attempts — bytes, exit code,
  stderr, timeout — and how to feed the data in yourself. That always works,
  because a redirect or a pipe makes `devin` write the whole document in one go:
  (and if it does not, `command -v devin` tells you whether devinp is even
  talking to the CLI and not to a shell function or agent wrapper around it).

  ```sh
  devin models list --format json > models.json && devinp --source models.json
  devin models list --format json | devinp --source=-
  ```

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
devin models list --format json | devinp --source=-   # feed it straight from stdin
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

`.github/workflows/publish.yml` publishes a patch release on every push to main
with [npm trusted publishing](https://docs.npmjs.org/trusted-publishers) (OIDC —
no `NPM_TOKEN`). Requires the trusted publisher to be configured on npmjs.com:
repository `lucasschirm/devinp`, workflow `publish.yml`.

It **never commits to main** — the branch ruleset requires every change to
arrive through a pull request, and a rejected release commit used to strand the
publish step that ran after it (GH013, then `npm publish` skipped). The released
version therefore lives in the npm registry and in the git tag, not in main's
`package.json`:

```sh
git ls-remote --tags origin                 # released versions
npm view @lucasschirm/devinp version       # latest published
```

The workflow takes the first patch number that is free in both places, so a tag
left behind by an older run cannot wedge it.

## License

MIT
