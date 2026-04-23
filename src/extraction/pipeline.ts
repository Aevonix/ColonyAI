/**
 * Lightweight turn extraction pipeline.
 *
 * Extracts topics, entities, tools_used, and summary from turn text.
 * The current implementation uses simple regex/keyword heuristics —
 * good enough for initial signal quality. An LLM-enhanced extraction
 * path can be swapped in later by replacing the extraction strategy
 * while keeping the same `ExtractionResult` interface.
 */

export interface ExtractionResult {
  topics: string[];
  entities: string[];
  tools_used: string[];
  summary: string;
  pending_tasks: string[];
}

/**
 * Common English stopwords to filter out for topic extraction.
 * Kept small to avoid over-filtering domain-specific terms.
 */
const STOPWORDS = new Set([
  "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
  "of", "with", "by", "from", "is", "it", "as", "be", "was", "were",
  "been", "are", "have", "has", "had", "do", "does", "did", "will",
  "would", "could", "should", "may", "might", "shall", "can", "not",
  "this", "that", "these", "those", "i", "you", "he", "she", "we",
  "they", "me", "him", "her", "us", "them", "my", "your", "his",
  "its", "our", "their", "what", "which", "who", "whom", "how",
  "when", "where", "why", "if", "then", "else", "so", "no", "yes",
  "just", "very", "also", "about", "up", "out", "all", "some",
  "any", "each", "every", "both", "few", "more", "most", "other",
  "such", "only", "same", "than", "too", "well", "here", "there",
  "now", "over", "into", "after", "before", "between", "under",
  "again", "further", "once", "during", "am", "being", "having",
  "doing", "because", "until", "while", "through", "above", "below",
]);

const MAX_TOPICS = 5;
const SUMMARY_MAX_CHARS = 200;

export class TurnExtractionPipeline {
  /**
   * Extract structured data from a turn's inbound and outbound text.
   */
  extract(inboundText: string, assistantText: string): ExtractionResult {
    const combined = [inboundText, assistantText].filter(Boolean).join("\n");

    return {
      topics: this.extractTopics(combined),
      entities: this.extractEntities(combined),
      tools_used: this.extractTools(assistantText),
      summary: this.extractSummary(assistantText),
      pending_tasks: this.extractPendingTasks(assistantText),
    };
  }

  /**
   * Extract pending tasks/commitments from assistant text.
   *
   * Looks for imperative-future patterns ("I'll X", "I will X"),
   * explicit TODO markers, and unchecked markdown checkboxes.
   * Returns up to 5 deduplicated, trimmed task strings.
   */
  private extractPendingTasks(text: string): string[] {
    if (!text) return [];
    const tasks: string[] = [];
    const seen = new Set<string>();

    const push = (raw: string) => {
      const t = raw.trim().replace(/\s+/g, " ");
      if (!t || t.length < 4 || t.length > 160) return;
      const key = t.toLowerCase();
      if (seen.has(key)) return;
      seen.add(key);
      tasks.push(t);
    };

    // "I'll / I will / I shall <action>" — capture up to sentence end or
    // the next conjoined "and I" clause (so we split multi-commitment lines).
    for (const m of text.matchAll(
      /\bI(?:'ll| will| shall)\s+(.+?)(?=\s+and\s+I\b|[.!?\n]|$)/gi,
    )) {
      if (m[1]) push(m[1]);
    }

    // Explicit TODO markers.
    for (const m of text.matchAll(/\bTODO:?\s*([^\n.!?]{3,160})/gi)) {
      if (m[1]) push(m[1]);
    }

    // Unchecked markdown checkboxes: "- [ ] item" or "* [ ] item".
    for (const m of text.matchAll(
      /(?:^|\n)\s*[-*]\s*\[\s\]\s*([^\n]{3,160})/g,
    )) {
      if (m[1]) push(m[1]);
    }

    // "Next step(s): X" / "Next: X"
    for (const m of text.matchAll(
      /\bnext(?:\s+steps?)?\s*[:\-]\s*([^\n.!?]{3,160})/gi,
    )) {
      if (m[1]) push(m[1]);
    }

    return tasks.slice(0, 5);
  }

  /**
   * Extract topics via keyword frequency analysis.
   *
   * Tokenizes text, filters stopwords and short tokens, counts
   * frequency, and returns the top N most frequent terms.
   */
  private extractTopics(text: string): string[] {
    const words = text
      .toLowerCase()
      .replace(/[^a-z0-9\s-]/g, " ")
      .split(/\s+/)
      .filter((w) => w.length > 2 && !STOPWORDS.has(w));

    const freq = new Map<string, number>();
    for (const w of words) {
      freq.set(w, (freq.get(w) ?? 0) + 1);
    }

    return [...freq.entries()]
      .sort((a, b) => b[1] - a[1])
      .slice(0, MAX_TOPICS)
      .map(([word]) => word);
  }

  /**
   * Extract entities by finding capitalized multi-word sequences.
   *
   * This is a simple approximation of named-entity recognition:
   * consecutive words starting with uppercase letters are treated as
   * proper nouns / entity names. Single capitalized words at the
   * start of a sentence are excluded heuristically.
   */
  private extractEntities(text: string): string[] {
    const entities: string[] = [];
    // Match 2+ consecutive capitalized words
    const multiWordRe = /\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b/g;
    let match: RegExpExecArray | null;
    while ((match = multiWordRe.exec(text)) !== null) {
      const entity = match[1]!;
      if (!entities.includes(entity)) {
        entities.push(entity);
      }
    }
    // Also catch single-word all-caps tokens (3+ chars, not common words)
    const singleCapsRe = /\b([A-Z]{3,})\b/g;
    while ((match = singleCapsRe.exec(text)) !== null) {
      const entity = match[1]!;
      if (!entities.includes(entity)) {
        entities.push(entity);
      }
    }
    return entities.slice(0, 10);
  }

  /**
   * Extract tool names from assistant text.
   *
   * Looks for common patterns:
   *  - `tool_name(` — function-call style
   *  - `"tool": "name"` or `"name": "tool_name"` — JSON-style
   *  - `used tool_name` or `called tool_name` — natural language
   */
  private extractTools(text: string): string[] {
    const tools = new Set<string>();

    // Pattern: function_call(
    for (const m of text.matchAll(/\b([a-z_][a-z0-9_]*)\s*\(/g)) {
      if (m[1]) tools.add(m[1]);
    }

    // Pattern: "tool": "name" or "function": "name"
    for (const m of text.matchAll(/"(?:tool|function|name)"\s*:\s*"([^"]+)"/g)) {
      if (m[1]) tools.add(m[1]);
    }

    // Pattern: called/used/invoked tool_name
    for (const m of text.matchAll(
      /\b(?:called|used|invoked|ran|executed)\s+([a-z_][a-z0-9_]*)\b/gi,
    )) {
      if (m[1]) tools.add(m[1]);
    }

    return [...tools].slice(0, 10);
  }

  /**
   * Extract a summary from assistant text.
   *
   * Takes the first ~200 characters, truncated at the last sentence
   * boundary (period, exclamation, or question mark followed by space
   * or end-of-string) to avoid cutting mid-sentence.
   */
  private extractSummary(text: string): string {
    if (text.length <= SUMMARY_MAX_CHARS) return text;

    const truncated = text.slice(0, SUMMARY_MAX_CHARS);
    // Find the last sentence boundary within the truncated text.
    let lastPos = -1;
    const re = /[.!?](?:\s|$)/g;
    let m: RegExpExecArray | null;
    while ((m = re.exec(truncated)) !== null) {
      lastPos = m.index + 1;
    }

    if (lastPos > 20) {
      return truncated.slice(0, lastPos).trim();
    }
    // No good sentence boundary — just truncate with ellipsis
    return truncated.trim() + "…";
  }
}
