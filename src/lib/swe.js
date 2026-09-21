import {readFile} from 'node:fs/promises'
import {fileURLToPath} from 'node:url'

const SCORES_URL = new URL('../../data/swe-scores.json', import.meta.url)

/**
 * Load data/swe-scores.json (written by scripts/update-swe.js).
 * Returns {models: {slug: {resolved, sweName, date}}, meta: {updated, source}}.
 * Missing file → empty models, so the CLI still works without scores.
 */
export async function loadSweScores() {
  try {
    const parsed = JSON.parse(await readFile(fileURLToPath(SCORES_URL), 'utf8'))
    return {
      models: parsed.models ?? {},
      meta: {updated: parsed.updated, source: parsed.source, unmatched: parsed.unmatched ?? []},
    }
  } catch {
    return {models: {}, meta: {}}
  }
}
