#!/usr/bin/env node

/**
 * Refresh data/swe-scores.json from the SWE-bench Verified leaderboard.
 *
 * Manual run only:  npm run update-swe
 *
 * The swebench.com index page embeds all leaderboards as JSON inside
 * <script type="application/json" id="leaderboard-data">. We take the
 * "Verified" board and keep the mini-SWE-agent ("Bash Only") entries —
 * the apples-to-apples LM comparison.
 */

import {execFile} from 'node:child_process'
import {mkdir, readFile, writeFile} from 'node:fs/promises'
import {dirname} from 'node:path'
import {fileURLToPath} from 'node:url'
import {promisify} from 'node:util'

const execFileAsync = promisify(execFile)

const SWEBENCH_URL = 'https://www.swebench.com/index.html'
const OUT_FILE = fileURLToPath(new URL('../data/swe-scores.json', import.meta.url))
const SAMPLE_FILE = fileURLToPath(new URL('../sample/models.json', import.meta.url))

/**
 * Manual overrides for leaderboard names that do not normalize to a family
 * slug. Key: normalize(leaderboard name), value: family slug.
 * Extend this when the run reports unmatched names below.
 */
const MANUAL_MAP = {
  // 'glm5': 'glm-5.3',
}

function normalize(name) {
  return name
    .toLowerCase()
    .replace(/\([^)]*\)/g, '')
    .replace(/[^a-z0-9]+/g, '')
}

async function fetchLeaderboard() {
  const res = await fetch(SWEBENCH_URL)
  if (!res.ok) throw new Error(`GET ${SWEBENCH_URL} → ${res.status}`)
  const html = await res.text()
  const match = /<script type="application\/json" id="leaderboard-data">([\s\S]*?)<\/script>/.exec(html)
  if (!match) throw new Error('leaderboard-data JSON blob not found — page structure changed?')
  const boards = JSON.parse(match[1])
  const verified = boards.find((b) => b.name === 'Verified')
  if (!verified) throw new Error('"Verified" leaderboard not found in leaderboard-data')
  return verified.results.filter((r) => r.agent === 'mini-SWE-agent')
}

async function loadFamilies() {
  try {
    const {stdout} = await execFileAsync('devin', ['models', 'list', '--format', 'json'], {
      maxBuffer: 64 * 1024 * 1024,
    })
    return JSON.parse(stdout).families
  } catch {
    console.warn('warning: `devin models list` unavailable, falling back to sample/models.json')
    return JSON.parse(await readFile(SAMPLE_FILE, 'utf8')).families
  }
}

function buildSlugIndex(families) {
  const index = new Map()
  for (const family of families) {
    for (const key of [family.family_label, family.slug, ...(family.aliases ?? [])]) {
      const normalized = normalize(key)
      if (normalized) index.set(normalized, family.slug)
    }
  }
  return index
}

const entries = await fetchLeaderboard()
const families = await loadFamilies()
const slugIndex = buildSlugIndex(families)

const models = {}
const unmatched = []
for (const entry of entries) {
  const key = normalize(entry.name)
  const slug = MANUAL_MAP[key] ?? slugIndex.get(key)
  if (!slug) {
    unmatched.push(entry.name)
    continue
  }
  const existing = models[slug]
  // Prefer entries checked by the SWE-bench team; otherwise keep the best score.
  const better =
    !existing ||
    (entry.checked && !existing.checked) ||
    (entry.checked === existing.checked && Number(entry.resolved) > existing.resolved)
  if (better) {
    models[slug] = {
      resolved: Number(entry.resolved),
      sweName: entry.name,
      checked: Boolean(entry.checked),
      date: entry.date ?? null,
    }
  }
}

const today = new Date().toISOString().slice(0, 10)
const output = {
  updated: today,
  source: `${SWEBENCH_URL} (Verified · mini-SWE-agent)`,
  models: Object.fromEntries(Object.entries(models).sort(([a], [b]) => a.localeCompare(b))),
  unmatched: unmatched.sort(),
}
await mkdir(dirname(OUT_FILE), {recursive: true})
await writeFile(OUT_FILE, JSON.stringify(output, null, 2) + '\n')

console.log(`wrote ${OUT_FILE}`)
console.log(`matched ${Object.keys(models).length} of ${entries.length} leaderboard entries to ${families.length} families`)
if (unmatched.length) {
  console.log(`unmatched (${unmatched.length}):`)
  for (const name of unmatched) console.log(`  - ${name}`)
  console.log('add entries to MANUAL_MAP in scripts/update-swe.js to map these')
}
