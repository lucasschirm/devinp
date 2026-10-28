#!/usr/bin/env node

/**
 * Assert the npm tarball never ships `bench/` (bench/ is a separate Python
 * package — see bench/PLAN.md §8.1) and that the shipped surfaces are
 * complete: every file the build produces in `bin/` and every file in
 * `data/` must appear in the tarball.
 *
 * The completeness check matters beyond "an entry under bin/ exists": npm
 * always ships the files referenced by the `bin` field even when `bin` is
 * dropped from `files`, so with a partial regression (`"files": ["data"]`)
 * the tarball would still contain `bin/run.js` but drop `bin/command.js`
 * — and the installed CLI crashes resolving oclif's
 * `commands.single.target`. Checking for `bin/` entries alone would pass
 * that broken package under green CI.
 *
 * Fails if:
 *   - any tarball entry sits under `bench/`, or
 *   - a file present in `bin/` or `data/` after the build is missing from
 *     the tarball, or
 *   - `package.json` is missing from the tarball.
 *
 * Usage: node scripts/check-pack.js
 *
 * `bin/` is generated and gitignored, so we build it first (mirroring
 * `prepack`); `--ignore-scripts` keeps the `--json` output clean.
 */

import {readdirSync, statSync} from 'node:fs'
import {execSync} from 'node:child_process'

function run(cmd) {
  execSync(cmd, {stdio: 'inherit'})
}

run('npm run build')

const out = execSync('npm pack --dry-run --json --ignore-scripts', {
  encoding: 'utf8',
})

function parsePackOutput(text) {
  // `npm pack --dry-run --json` normally emits a single-element array (some
  // npm versions wrap it in an object); normalize to the entry object.
  const candidates = [text]
  const start = text.search(/^[\[{]/m)
  if (start !== -1) candidates.push(text.slice(start))
  for (const candidate of candidates) {
    try {
      const parsed = JSON.parse(candidate)
      return Array.isArray(parsed) ? parsed[0] : parsed
    } catch {
      // try the next candidate
    }
  }
  console.error('FAIL: no parseable JSON block in npm pack output')
  console.error(text)
  process.exit(1)
}

const entry = parsePackOutput(out)
const paths = (entry?.files ?? []).map(f => f.path)

const failures = []

const benchEntries = paths.filter(
  p => p === 'bench' || p.startsWith('bench/') || p.includes('/bench/'),
)
if (benchEntries.length) {
  failures.push(
    `tarball contains bench/ entries:\n  ${benchEntries.join('\n  ')}`,
  )
}

// Positive guards, fail-closed: every file the build left in bin/ (and
// every file in data/) must ship. See the header for why "has a bin/
// entry" is not enough.
for (const dir of ['bin', 'data']) {
  const onDisk = readdirSync(dir, {recursive: true})
    .filter(f => statSync(`${dir}/${f}`).isFile())
    .sort()
  const packed = new Set(
    paths.filter(p => p.startsWith(`${dir}/`)).map(p => p.slice(dir.length + 1)),
  )
  const missing = onDisk.filter(f => !packed.has(f))
  if (missing.length) {
    failures.push(`${dir}/ files missing from tarball: ${missing.join(', ')}`)
  }
}

if (!paths.includes('package.json')) failures.push('tarball missing package.json')

if (failures.length) {
  console.error('FAIL:')
  for (const f of failures) console.error(`  - ${f}`)
  process.exit(1)
}

console.log(
  `ok: ${paths.length} tarball entries — no bench/ entries; bin/ + data/ complete`,
)
console.log(`    ${paths.join(', ')}`)
