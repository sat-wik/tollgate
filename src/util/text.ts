/** Normalize text for similarity/prefix comparison: lowercase, collapse runs of
 * whitespace to a single space, and trim. Deterministic and pure. */
export function normalizeText(s: string): string {
  return s.toLowerCase().replace(/\s+/g, " ").trim();
}

/** Word-level k-shingles of normalized text, as a Set of joined strings. */
export function shingles(normalized: string, k = 5): Set<string> {
  const words = normalized.split(" ").filter(Boolean);
  const set = new Set<string>();
  if (words.length < k) {
    if (words.length) set.add(words.join(" "));
    return set;
  }
  for (let i = 0; i <= words.length - k; i++) {
    set.add(words.slice(i, i + k).join(" "));
  }
  return set;
}

/** Jaccard similarity between two sets (|A∩B| / |A∪B|). */
export function jaccard<T>(a: Set<T>, b: Set<T>): number {
  if (a.size === 0 && b.size === 0) return 1;
  let inter = 0;
  for (const x of a) if (b.has(x)) inter++;
  const union = a.size + b.size - inter;
  return union === 0 ? 0 : inter / union;
}

/** Length (in characters) of the common prefix of two strings. */
export function commonPrefixLength(a: string, b: string): number {
  const n = Math.min(a.length, b.length);
  let i = 0;
  while (i < n && a[i] === b[i]) i++;
  return i;
}
