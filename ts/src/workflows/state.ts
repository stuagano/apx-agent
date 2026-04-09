/**
 * AgentState — shared key-value store for workflow agents.
 *
 * Follows the Google ADK pattern:
 * - `output_key`: when an agent has an outputKey, its result is stored under that key
 * - Template interpolation: `{variable_name}` in instruction strings resolves from state
 * - Scoped state: keys prefixed with `temp:` are turn-specific and cleared between steps
 *
 * @example
 * const state = new AgentState({ topic: 'billing' });
 * state.set('analysis', 'The billing data shows...');
 *
 * // Template interpolation
 * const instructions = state.interpolate('Summarize the {topic} analysis: {analysis}');
 * // => 'Summarize the billing analysis: The billing data shows...'
 *
 * // Scoped temp values
 * state.set('temp:scratchpad', 'intermediate work');
 * state.clearTemp();
 * state.has('temp:scratchpad'); // false
 */

/** Prefix for turn-scoped keys that get cleared between steps. */
const TEMP_PREFIX = 'temp:';

export class AgentState {
  private store: Map<string, unknown>;

  constructor(initial?: Record<string, unknown>) {
    this.store = new Map(initial ? Object.entries(initial) : []);
  }

  /** Get a value by key. Returns undefined if not present. */
  get<T = unknown>(key: string): T | undefined {
    return this.store.get(key) as T | undefined;
  }

  /** Set a value. Use `temp:` prefix for turn-scoped data. */
  set(key: string, value: unknown): void {
    this.store.set(key, value);
  }

  /** Check if a key exists. */
  has(key: string): boolean {
    return this.store.has(key);
  }

  /** Delete a key. */
  delete(key: string): boolean {
    return this.store.delete(key);
  }

  /** Return all keys. */
  keys(): string[] {
    return Array.from(this.store.keys());
  }

  /** Return all entries as a plain object. */
  toObject(): Record<string, unknown> {
    return Object.fromEntries(this.store);
  }

  /**
   * Clear all keys with the `temp:` prefix.
   * Called between agent steps in a sequential pipeline so
   * temporary scratchpad data doesn't leak across turns.
   */
  clearTemp(): void {
    for (const key of this.store.keys()) {
      if (key.startsWith(TEMP_PREFIX)) {
        this.store.delete(key);
      }
    }
  }

  /**
   * Replace `{variable_name}` placeholders in a string with state values.
   *
   * Only replaces variables that exist in state. Unknown placeholders are
   * left as-is so downstream agents can still reference them (or the caller
   * gets a clear signal that the variable wasn't set).
   *
   * Values are coerced to strings via `String()`.
   */
  interpolate(template: string): string {
    return template.replace(/\{(\w+(?::\w+)?)\}/g, (match, key: string) => {
      if (this.store.has(key)) {
        return String(this.store.get(key));
      }
      return match; // leave unresolved placeholders intact
    });
  }

  /** Create a shallow copy of this state. */
  clone(): AgentState {
    const copy = new AgentState();
    for (const [k, v] of this.store) {
      copy.set(k, v);
    }
    return copy;
  }
}
