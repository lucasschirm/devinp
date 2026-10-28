#!/usr/bin/env node

/**
 * Assert the npm tarball never ships `bench/` (bench/ is a separate Python
 * package — see bench/PLAN.md §8.1), that the shipped surfaces are
 * complete, and that nothing outside the expected set ships at all.
 *
 * The completeness check compares against the built tree rather than just
 * "an entry under bin/ exists": npm always ships the files referenced by
 * the `bin` field even when `bin` is dropped from `files`, so with a
 * partial regression (`"files": ["data"]`) the tarball would still contain
 * `bin/run.js` but drop `bin/command.js` — and the installed CLI crashes
 * resolving oclif's `commands.single.target`. Checking for `bin/` entries
 * alone would pass that broken package under green CI.
 *
 * Fails if:
 *   - any tarball entry sits under `bench/`, or
 *   - `bin/` or `data/` is empty on disk after the build (the build must
 *     produce its outputs — an empty tree would otherwise pass vacuously),
 *   - a file present in `bin/` or `data/` after the build is missing from
 *     the tarball, or
 *   - `package.json` is missing from the tarball, or
 *   - a tarball entry lies outside the expected set (auto-included files +
 *     `bin/` + `data/`), e.g. `src/`, `test/`, `sample/`.
 *
 * Usage: node scripts/check-pack.js
 *
 * `bin/` is generated and gitignored, so we build it first (mirroring
 * `prepack`); `--ignore-scripts` keeps the `--json` output clean.
 *
 * Robustness: this check is fail-closed by construction — if a future npm
 * changes the `pack --json` shape such that no file list can be extracted,
 * every guard below fails and the job/step goes red. It never passes
 * silently on an unrecognizable output.
 */

import {readdirSync, statSync} from 'node:fs'
import {execSync} from 'node:child_process'
import {pathToFileURL} from 'node:url'

// Files npm never packs (npm pack docs — "always ignored"), so they cannot
// be required in the tarball even when present on disk.
const ALWAYS_IGNORED = [
  /^\.npmrc$/,
  /^npm-debug\.log/,
  /^\.DS_Store$/,
  /^package-lock\.json$/,
  /\.orig$/,
]

// Expected tarball content, allowlist: what npm auto-includes (package.json,
// README.md, LICENSE) plus the directories package.json "files" points at
// (bin/, data/). Anything else is a regression.
const EXPECTED_TOP_LEVEL = new Set(['package.json', 'README.md', 'LICENSE'])

function filterIgnored(files) {
  return files.filter(f => !ALWAYS_IGNORED.some((re) => re.test(f)))
}

/**
 * Normalize `npm pack --dry-run --json` output across npm majors to the
 * entry object carrying `files`:
 *   npm 11: single-element array  [{ id, files: [...], ... }]
 *   npm 12: object keyed by package name
 *          { "@lucasschirm/devinp": { id, files: [...], ... } }
 */
export function extractEntry(parsed) {
  if (Array.isArray(parsed)) return parsed[0]
  if (parsed?.files) return parsed
  const withFiles = Object.values(parsed ?? {}).find((v) => v?.files)
  return withFiles ?? parsed
}

/** Parse the CLI output; returns the entry object, or null when no
 *  parseable JSON block can be found (the caller reports the failure). */
export function parsePackOutput(text) {
  const candidates = [text]
  const start = text.search(/^[\[{]/m)
  if (start !== -1) candidates.push(text.slice(start))
  for (const candidate of candidates) {
    try {
      return extractEntry(JSON.parse(candidate))
    } catch {
      // try the next candidate
    }
  }
  return null
}

/**
 * Check a pack file list against the on-disk state of bin/ and data/.
 * `paths` are the tarball entry paths; `binFiles`/`dataFiles` are the file
 * names present in those directories after the build. Returns the list of
 * failure strings (empty when the tarball is sound).
 */
export function checkTarball(paths, {binFiles, dataFiles}) {
  const failures = []

  const benchEntries = paths.filter(
    (p) => p === 'bench' || p.startsWith('bench/') || p.includes('/bench/'),
  )
  if (benchEntries.length) {
    failures.push(`tarball contains bench/ entries:\n  ${benchEntries.join('\n  ')}`)
  }

  if (!paths.includes('package.json')) failures.push('tarball missing package.json')

  // Positive guards, fail-closed: every file the build left in bin/ (and
  // every file in data/) must ship — see the header for why "has a bin/
  // entry" is not enough.
  for (const [dir, onDiskRaw] of [
    ['bin', binFiles],
    ['data', dataFiles],
  ]) {
    const onDisk = filterIgnored(onDiskRaw).sort()
    if (onDisk.length === 0) {
      failures.push(`${dir}/ is empty on disk after the build`)
      continue
    }
    const packed = new Set(
      paths.filter((p) => p.startsWith(`${dir}/`)).map((p) => p.slice(dir.length + 1)),
    )
    const missing = onDisk.filter((f) => !packed.has(f))
    if (missing.length) {
      failures.push(`${dir}/ files missing from tarball: ${missing.join(', ')}`)
    }
  }

  // Allowlist: nothing outside the expected set may ship (src/, test/,
  // sample/, … would otherwise pass silently under a "files" regression).
  const unexpected = paths.filter((p) => {
    if (p.startsWith('bin/') || p.startsWith('data/')) return false
    return !EXPECTED_TOP_LEVEL.has(p)
  })
  if (unexpected.length) {
    failures.push(`unexpected tarball entries: ${unexpected.join(', ')}`)
  }

  return failures
}

function listFiles(dir) {
  return readdirSync(dir, {recursive: true})
    .filter((f) => statSync(`${dir}/${f}`).isFile())
}

// --- main (skipped when imported as a module by the tests) ---
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  execSync('npm run build', {stdio: 'inherit'})

  const out = execSync('npm pack --dry-run --json --ignore-scripts', {
    encoding: 'utf8',
  })

  const entry = parsePackOutput(out)
  if (!entry) {
    console.error('FAIL: no parseable JSON block in npm pack output')
    console.error(out)
    process.exit(1)
  }

  const paths = (entry?.files ?? []).map((f) => f.path)
  const failures = checkTarball(paths, {
    binFiles: listFiles('bin'),
    dataFiles: listFiles('data'),
  })

  if (failures.length) {
    console.error('FAIL:')
    for (const f of failures) console.error(`  - ${f}`)
    process.exit(1)
  }

  console.log(
    `ok: ${paths.length} tarball entries — no bench/ entries; bin/ + data/ complete`,
  )
  console.log(`    ${paths.join(', ')}`)
}
