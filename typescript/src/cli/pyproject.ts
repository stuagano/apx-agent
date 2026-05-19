/**
 * pyproject.ts — read `[tool.apx.agent]` defaults from `./pyproject.toml`.
 *
 * Mirrors the Python `_read_apx_agent_config` helper in `cli.py`. Used by
 * commands so users don't have to repeat `--model NAME` on every invocation
 * — set it once under `[tool.apx.agent]` and the CLI picks it up.
 *
 * # Parser choice — regex, not TOML library
 *
 * `[tool.apx.agent]` is flat: every key is a string (`name`, `description`,
 * `model`, `instructions`, `experiment`). There are no nested tables, arrays
 * of tables, or inline tables inside this section in any apx-agent project
 * we've seen. A regex-based section scanner handles every real shape and
 * avoids a new runtime dependency. The trade-off is documented limitations:
 *
 *   - No nested tables inside `[tool.apx.agent]`.
 *   - No arrays / inline-tables.
 *   - String values only (single- or double-quoted, single line).
 *   - Comments (`# ...`) trailing a value line are stripped.
 *
 * If we ever need full TOML semantics here, swap to `smol-toml` — the public
 * surface (`readApxAgentConfig`) stays the same.
 */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';

/** Plain key/value map of `[tool.apx.agent]` settings. */
export type ApxAgentConfig = Record<string, unknown>;

/**
 * Read `[tool.apx.agent]` from a `pyproject.toml` file.
 *
 * @param pyprojectPath Optional explicit path. Defaults to `./pyproject.toml`
 *   relative to the current working directory.
 * @returns A flat map of the settings under the section. Empty object when
 *   the file is missing, unreadable, or has no `[tool.apx.agent]` table.
 */
export function readApxAgentConfig(pyprojectPath?: string): ApxAgentConfig {
  const path = pyprojectPath ?? join(process.cwd(), 'pyproject.toml');
  let raw: string;
  try {
    raw = readFileSync(path, 'utf8');
  } catch {
    return {};
  }
  return parseApxAgentSection(raw);
}

/**
 * Pull `[tool.apx.agent]` out of a TOML string. Exported for tests; prefer
 * {@link readApxAgentConfig} in callers.
 */
export function parseApxAgentSection(toml: string): ApxAgentConfig {
  const lines = toml.split(/\r?\n/);
  const HEADER = /^\s*\[tool\.apx\.agent\]\s*$/;
  const ANY_HEADER = /^\s*\[[^\]]+\]\s*$/;
  // key = value — quoted string values only (single or double quotes).
  const KV = /^\s*([A-Za-z_][A-Za-z0-9_\-]*)\s*=\s*(.+?)\s*$/;

  let inSection = false;
  const out: ApxAgentConfig = {};

  for (const lineRaw of lines) {
    const line = lineRaw.replace(/\s+$/, '');
    if (HEADER.test(line)) {
      inSection = true;
      continue;
    }
    if (inSection && ANY_HEADER.test(line)) {
      // Entered a new section — stop scanning.
      break;
    }
    if (!inSection) continue;
    if (line.trim() === '' || line.trim().startsWith('#')) continue;

    const m = KV.exec(line);
    if (!m) continue;
    const key = m[1]!;
    const valuePart = stripTrailingComment(m[2]!);
    const parsed = parseScalar(valuePart);
    if (parsed !== undefined) out[key] = parsed;
  }

  return out;
}

function stripTrailingComment(value: string): string {
  // Only strip `#` comments that are clearly outside a quoted string.
  let inSingle = false;
  let inDouble = false;
  for (let i = 0; i < value.length; i++) {
    const ch = value[i];
    if (ch === "'" && !inDouble) inSingle = !inSingle;
    else if (ch === '"' && !inSingle) inDouble = !inDouble;
    else if (ch === '#' && !inSingle && !inDouble) {
      return value.slice(0, i).trimEnd();
    }
  }
  return value;
}

function parseScalar(value: string): unknown {
  const trimmed = value.trim();
  if (trimmed.length === 0) return undefined;
  if (
    (trimmed.startsWith('"') && trimmed.endsWith('"')) ||
    (trimmed.startsWith("'") && trimmed.endsWith("'"))
  ) {
    // Bare string — strip the matching quotes. No escape handling beyond
    // what TOML basic strings need for this section's values.
    return trimmed.slice(1, -1);
  }
  if (/^-?\d+$/.test(trimmed)) {
    return Number(trimmed);
  }
  if (trimmed === 'true' || trimmed === 'false') {
    return trimmed === 'true';
  }
  // Unsupported shape (array, inline table, multiline) — skip rather than
  // misparse. Callers see a missing key and either fall back to a default
  // or surface a usage error.
  return undefined;
}
