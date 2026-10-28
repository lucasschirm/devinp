#!/usr/bin/env node

/**
 * Assert the npm tarball never ships `bench/` (bench/ is a separate Python
 * package — see bench/PLAN.md §8.1).
 *
 * Runs `npm pack --dry-run` and fails if any tarball entry sits under
 * `bench/`. Positive guards also fail closed if the shipped surfaces
 * (`bin/`, `data/`) or `package.json` are missing, so a regression in
 * `package.json` `"files"` (e.g. an emptied array) cannot pass vacuously.
 *
 * Usage: node scripts/check-pack.js
 *
 * `bin/` is generated and gitignored, so we build it first (mirroring
 * `prepack`); `--ignore-scripts` keeps the `--json` output clean.
 */

import {execSync} from 'node:child_process'

function run(cmd) {
  execSync(cmd, {stdio: 'inherit'})
}

run('npm run build')

const out = execSync('npm pack --dry-run --json --ignore-scripts', {
  encoding: 'utf8',
})

// `npm pack --dry-run --json` emits a single-element array (some npm
// versions wrap it in an object); normalize to the entry object.
let parsed
try {
  parsed = JSON.parse(out)
} catch {
  // Fall back to the last JSON block in case of leading stdout noise.
  const start = out.search(/^[\[{]/m)
  parsed = JSON.parse(out.slice(start))
}
const entry = Array.isArray(parsed) ? parsed[0] : parsed
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

// Positive guards: fail closed if shipped surfaces vanished.
const has = prefix =>
  paths.some(p => p === prefix || p.startsWith(`${prefix}/`))
if (!has('bin')) failures.push('tarball has no bin/* entries (prepack/build ran?)')
if (!has('data')) failures.push('tarball has no data/* entries')
if (!paths.includes('package.json')) failures.push('tarball missing package.json')

if (failures.length) {
  console.error('FAIL:')
  for (const f of failures) console.error(`  - ${f}`)
  process.exit(1)
}

console.log(
  `ok: ${paths.length} tarball entries — no bench/ entries; bin/, data/, package.json present`,
)
console.log(`    ${paths.join(', ')}`)
