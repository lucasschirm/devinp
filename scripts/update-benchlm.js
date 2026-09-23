#!/usr/bin/env node

/**
 * Refresh data/benchlm-scores.json from the BenchLM leaderboard API.
 *
 * Manual run only:  npm run update-benchlm
 *
 * BenchLM (benchlm.ai) aggregates benchmark results into per-category
 * scores. We take the "coding" category board and keep the entries that
 * map to a Devin model family. The default page size is 50; LIMIT is raised
 * so the long tail (smaller families further down the board) is included.
 */

import {execFile} from 'node:child_process'
import {mkdir, readFile, writeFile} from 'node:fs/promises'
import {dirname} from 'node:path'
import {fileURLToPath} from 'node:url'
import {promisify} from 'node:util'

const execFileAsync = promisify(execFile)

const BENCHLM_URL = 'https://benchlm.ai/api/data/leaderboard'
const LIMIT = 200
const OUT_FILE = fileURLToPath(new URL('../data/benchlm-scores.json', import.meta.url))
const SAMPLE_FILE = fileURLToPath(new URL('../sample/models.json', import.meta.url))

/**
 * Manual overrides for BenchLM model names that do not normalize to a family
 * slug. Key: normalize(model name), value: family slug.
 * Extend this when the run reports unmatched names below.
 */
const MANUAL_MAP = {}

function normalize(name) {
  return name
    .toLowerCase()
    .replace(/\([^)]*\)/g, '')
    .replace(/[^a-z0-9]+/g, '')
}

/**
 * Order-insensitive signature: "GPT-5.3 Codex" and "Codex GPT-5.3" both
 * → "5.3|codex|gpt". Catches word-order differences between BenchLM names
 * and family labels that exact normalized equality would miss.
 */
function signature(name) {
  const bare = name.toLowerCase().replace(/\([^)]*\)/g, '')
  return (bare.match(/\d+\.\d+|\d+|[a-z]+/g) ?? []).sort().join('|')
}

async function fetchBoard() {
  const url = `${BENCHLM_URL}?category=coding&limit=${LIMIT}`
  const res = await fetch(url)
  if (!res.ok) throw new Error(`GET ${url} → ${res.status}`)
  return res.json()
}

async function fetchBenchlm() {
  const board = await fetchBoard()
  const merged = new Map()
  for (const model of board.models ?? []) {
    const score = model.categoryScores?.coding
    if (score == null) continue
    merged.set(model.model, {score, supported: model.evidenceStatus === 'supported'})
  }
  return {merged, updated: board.lastUpdated ?? null}
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
  const exact = new Map()
  const bySignature = new Map()
  for (const family of families) {
    for (const key of [family.family_label, family.slug, ...(family.aliases ?? [])]) {
      const normalized = normalize(key)
      if (normalized) exact.set(normalized, family.slug)
    }
    // Signature index deliberately skips aliases: they are single loose
    // words ("claude", "gemini") that must not drive fuzzy matching.
    for (const key of [family.family_label, family.slug]) {
      const sig = signature(key)
      if (!sig) continue
      const existing = bySignature.get(sig)
      bySignature.set(sig, !existing || existing === family.slug ? family.slug : null)
    }
  }
  return {exact, bySignature}
}

const {merged, updated} = await fetchBenchlm()
const families = await loadFamilies()
const {exact, bySignature} = buildSlugIndex(families)

const models = {}
const unmatched = []
for (const [name, record] of merged) {
  const key = normalize(name)
  const slug = MANUAL_MAP[key] ?? exact.get(key) ?? bySignature.get(signature(name))
  if (!slug) {
    unmatched.push(name)
    continue
  }
  const existing = models[slug]
  // Prefer entries with supported evidence; otherwise keep the best score.
  const better =
    !existing ||
    (record.supported && !existing.supported) ||
    (record.supported === existing.supported && record.score > existing.score)
  if (better) {
    models[slug] = {
      score: record.score,
      benchName: name,
      supported: record.supported,
    }
  }
}

const output = {
  updated: updated ?? new Date().toISOString().slice(0, 10),
  source: `${BENCHLM_URL}?category=coding&limit=${LIMIT}`,
  models: Object.fromEntries(Object.entries(models).sort(([a], [b]) => a.localeCompare(b))),
  unmatched: unmatched.sort(),
}
await mkdir(dirname(OUT_FILE), {recursive: true})
await writeFile(OUT_FILE, JSON.stringify(output, null, 2) + '\n')

console.log(`wrote ${OUT_FILE}`)
console.log(`matched ${Object.keys(models).length} of ${merged.size} BenchLM models to ${families.length} families`)
if (unmatched.length) {
  console.log(`unmatched (${unmatched.length}):`)
  for (const name of unmatched) console.log(`  - ${name}`)
  console.log('add entries to MANUAL_MAP in scripts/update-benchlm.js to map these')
}
