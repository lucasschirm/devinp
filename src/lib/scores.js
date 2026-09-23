import {readFile} from 'node:fs/promises'
import {join} from 'node:path'

/**
 * Load data/benchlm-scores.json (written by scripts/update-benchlm.js),
 * resolved from the package root so it works both unbundled (src/) and
 * bundled (bin/). Returns {models: {slug: {score, benchName}}, meta:
 * {updated, source}}. Missing file → empty models, so the CLI still works
 * without scores.
 */
export async function loadScores(root) {
  const path = join(root, 'data', 'benchlm-scores.json')
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
