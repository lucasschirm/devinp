import {readFile} from 'node:fs/promises'
import {join} from 'node:path'

/**
 * Load data/swe-scores.json (written by scripts/update-swe.js), resolved
 * from the package root so it works both unbundled (src/) and bundled (bin/).
 * Returns {models: {slug: {resolved, sweName, date}}, meta: {updated, source}}.
 * Missing file → empty models, so the CLI still works without scores.
 */
export async function loadSweScores(root) {
  const path = join(root, 'data', 'swe-scores.json')
  try {
    const parsed = JSON.parse(await readFile(path, 'utf8'))
    return {
      models: parsed.models ?? {},
      meta: {updated: parsed.updated, source: parsed.source, unmatched: parsed.unmatched ?? []},
    }
  } catch {
    return {models: {}, meta: {}}
  }
}
