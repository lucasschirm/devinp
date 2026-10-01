const CLOSER = {'{': '}', '[': ']'}

/**
 * Best-effort recovery of a JSON document that was truncated mid-stream.
 *
 * `devin` occasionally dies before it has finished writing, leaving a
 * trailing partial element. This walks the text, finds the last safe cut
 * point (after a comma, after a container closer, or before an
 * unterminated string), closes the still-open containers, and re-parses.
 *
 * Returns the recovered value, or null when the input cannot be repaired.
 */
export function recoverTruncatedJson(raw) {
  if (typeof raw !== 'string') return null

  // Fast path — some truncations only drop closing brackets.
  try {
    return JSON.parse(raw)
  } catch {
    // fall through to repair
  }

  const scan = scanJson(raw, raw.length)
  const cuts = [raw.length]
  if (scan.inString) cuts.push(scan.stringStart)
  if (scan.lastComma != null) cuts.push(scan.lastComma + 1)
  if (scan.lastCloser != null) cuts.push(scan.lastCloser + 1)

  const seen = new Set()
  for (const cut of cuts) {
    if (seen.has(cut)) continue
    seen.add(cut)
    let candidate = raw.slice(0, cut).replace(/[\s,]+$/g, '')
    if (candidate === '') continue
    const s = scanJson(candidate, candidate.length)
    const repaired = candidate + s.stack.map((token) => CLOSER[token]).reverse().join('')
    try {
      return JSON.parse(repaired)
    } catch {
      // try the next cut
    }
  }
  return null
}

/**
 * Single pass over `s[0..end)`: tracks string/escape state, the stack of
 * open containers, and the last `,` / closer seen outside a string.
 */
function scanJson(s, end) {
  let inString = false
  let escape = false
  let stringStart = 0
  const stack = []
  let lastComma = null
  let lastCloser = null
  for (let i = 0; i < end; i++) {
    const ch = s[i]
    if (inString) {
      if (escape) escape = false
      else if (ch === '\\') escape = true
      else if (ch === '"') inString = false
      continue
    }
    if (ch === '"') {
      inString = true
      stringStart = i
    } else if (ch === ',') {
      lastComma = i
    } else if (ch === '{' || ch === '[') {
      stack.push(ch)
    } else if (ch === '}' || ch === ']') {
      stack.pop()
      lastCloser = i
    }
  }
  return {inString, stringStart, stack, lastComma, lastCloser}
}
