/**
 * Compare a pasted configuration with the one this run calculated.
 *
 * Three input shapes are read:
 *
 * - a `postgresql.conf` as it is on disk: comments are dropped, `include`
 *   directives are skipped, and the last assignment of a name wins, which is
 *   how the server reads the file;
 * - a delimited export of `pg_settings` with a header row: `name`, the first of
 *   `setting`/`current_setting`/`value`/`reset_val`/`boot_val`, and `unit` when
 *   present are used, the rest is ignored;
 * - the same export without a header: the first column is the name, the second
 *   the value, and a third is the unit when it looks like one.
 *
 * psql's aligned output — a `|` table with a dashed rule and a `(N rows)`
 * footer — reads as the delimited form. The delimiter is whichever of comma,
 * tab, semicolon and pipe most lines share.
 *
 * Values are compared after normalisation through the snapshot's metadata, so
 * `shared_buffers = 8GB`, `8388608kB` and pg_settings' `1048576` (in 8kB
 * pages) are one value, not three. Nothing here touches the DOM.
 */

import { numericValueInSettingUnits } from './make-conf.js';

const NAME_PATTERN = /^[A-Za-z_][A-Za-z0-9_.]*$/;
const CONF_ASSIGNMENT = /^([A-Za-z_][A-Za-z0-9_.]*)\s*(?:=\s*|\s+)(.*)$/;
const CONF_DIRECTIVES = new Set(['include', 'include_dir', 'include_if_exists']);
const DELIMITERS = [',', '\t', ';', '|'];
const UNIT_PATTERN = /^(8kB|16MB|kB|MB|GB|TB|B|us|ms|s|min|h|d)$/;
const NUMERIC_PATTERN = /^([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*([A-Za-z]+)?$/;
const HEADER_VALUE_COLUMNS = ['setting', 'current_setting', 'value', 'reset_val', 'boot_val'];
const TRUE_WORDS = new Set(['on', 'true', 'yes', '1']);
const FALSE_WORDS = new Set(['off', 'false', 'no', '0']);

/** The part of a postgresql.conf line before any `#` that is not inside quotes. */
function stripConfComment(line) {
  let quoted = false;
  for (let index = 0; index < line.length; index += 1) {
    const char = line[index];
    if (char === "'") quoted = !quoted;
    else if (char === '#' && !quoted) return line.slice(0, index);
  }
  return line;
}

/** A single-quoted conf value without its quotes; `''` and `\'` are one quote. */
export function unquoteConfValue(value) {
  const text = value.trim();
  if (text.length < 2 || !text.startsWith("'")) return text;
  let out = '';
  for (let index = 1; index < text.length; index += 1) {
    const char = text[index];
    if (char === '\\' && text[index + 1] === "'") {
      out += "'";
      index += 1;
    } else if (char === "'") {
      if (text[index + 1] === "'") {
        out += "'";
        index += 1;
      } else {
        return out;
      }
    } else {
      out += char;
    }
  }
  return out;
}

/** postgresql.conf text as the server would read it: last assignment wins. */
export function parseConfText(text) {
  const entries = new Map();
  let skipped = 0;
  for (const rawLine of text.split(/\r?\n/)) {
    const line = stripConfComment(rawLine).trim();
    if (!line) continue;
    const match = CONF_ASSIGNMENT.exec(line);
    if (match === null || CONF_DIRECTIVES.has(match[1])) {
      skipped += 1;
      continue;
    }
    entries.set(match[1], { value: unquoteConfValue(match[2]), unit: null });
  }
  return { entries, skipped };
}

/** One delimited line into cells; double quotes wrap a cell, `""` is one quote. */
function splitDelimited(line, delimiter) {
  const cells = [];
  let cell = '';
  let quoted = false;
  for (let index = 0; index < line.length; index += 1) {
    const char = line[index];
    if (quoted) {
      if (char === '"' && line[index + 1] === '"') {
        cell += '"';
        index += 1;
      } else if (char === '"') {
        quoted = false;
      } else {
        cell += char;
      }
    } else if (char === '"') {
      quoted = true;
    } else if (char === delimiter) {
      cells.push(cell);
      cell = '';
    } else {
      cell += char;
    }
  }
  cells.push(cell);
  return cells.map((item) => item.trim());
}

/** A pg_settings export, with or without a header row, into name → value. */
export function parseDelimitedText(text, delimiter) {
  const entries = new Map();
  let skipped = 0;
  let header = false;
  let columns = { name: 0, value: 1, unit: null };
  let first = true;
  for (const rawLine of text.split(/\r?\n/)) {
    if (!rawLine.trim()) continue;
    const cells = splitDelimited(rawLine, delimiter);
    if (first) {
      first = false;
      const lowered = cells.map((cell) => cell.toLowerCase());
      const valueColumn = HEADER_VALUE_COLUMNS.find((column) => lowered.includes(column));
      if (lowered.includes('name') && valueColumn !== undefined) {
        header = true;
        columns = {
          name: lowered.indexOf('name'),
          value: lowered.indexOf(valueColumn),
          unit: lowered.includes('unit') ? lowered.indexOf('unit') : null,
        };
        continue;
      }
    }
    const name = cells[columns.name];
    const value = cells[columns.value];
    if (name === undefined || value === undefined || !NAME_PATTERN.test(name)) {
      skipped += 1;
      continue;
    }
    let unit = null;
    if (columns.unit !== null) unit = cells[columns.unit] || null;
    else if (cells.length > 2 && UNIT_PATTERN.test(cells[2])) unit = cells[2];
    entries.set(name, { value, unit });
  }
  return { entries, skipped, header };
}

/** Which of the accepted shapes a text is, judged by what most lines look like. */
export function detectFormat(text) {
  const lines = text.split(/\r?\n/).filter((line) => line.trim() && !line.trim().startsWith('#'));
  if (!lines.length) return { format: 'empty', delimiter: null };
  const assignments = lines.filter((line) => /^\s*[A-Za-z_][A-Za-z0-9_.]*\s*=\s*\S/.test(line));
  if (assignments.length * 2 >= lines.length) return { format: 'conf', delimiter: null };
  let best = null;
  for (const delimiter of DELIMITERS) {
    const count = lines.filter((line) => line.includes(delimiter)).length;
    if (count * 2 >= lines.length && (best === null || count > best.count)) {
      best = { delimiter, count };
    }
  }
  if (best !== null) return { format: 'csv', delimiter: best.delimiter };
  // The `name value` form of a conf line: one bare or quoted token, not prose.
  const pairs = lines.filter((line) =>
    /^\s*[A-Za-z_][A-Za-z0-9_.]*\s+(?:'[^']*'|\S+)\s*(?:#.*)?$/.test(line),
  );
  if (pairs.length * 2 >= lines.length) return { format: 'conf', delimiter: null };
  return { format: 'unknown', delimiter: null };
}

/**
 * Read whatever was pasted. `format` is `conf`, `csv`, `empty` or `unknown`;
 * `header` says whether a csv carried column names; `skipped` counts the lines
 * that were neither blank nor readable.
 */
export function parseUserConfig(text) {
  const cleaned = String(text ?? '').replace(/^\uFEFF/, '');
  const { format, delimiter } = detectFormat(cleaned);
  if (format === 'conf') return { format, header: false, ...parseConfText(cleaned) };
  if (format === 'csv') return { format, delimiter, ...parseDelimitedText(cleaned, delimiter) };
  return { format, header: false, entries: new Map(), skipped: 0 };
}

function stripSingleQuotes(text) {
  return text.startsWith("'") && text.endsWith("'") && text.length >= 2
    ? unquoteConfValue(text)
    : text;
}

function numericKey(value) {
  return String(Math.round(value * 1e6) / 1e6);
}

/**
 * A comparison key for one value of one setting. Two spellings of the same
 * value give the same key; anything unreadable compares as the text itself.
 * `sourceUnit` is the unit a bare number is in when the input said so (the
 * pg_settings `unit` column); otherwise a bare number is in the snapshot's
 * unit, which is how postgresql.conf reads it.
 */
export function normalizeSettingValue(rawValue, sourceUnit, metadata) {
  const text = stripSingleQuotes(String(rawValue ?? '').trim());
  const vartype = metadata?.vartype ?? '';
  const lowered = text.toLowerCase();

  if (vartype === 'bool') {
    if (TRUE_WORDS.has(lowered)) return 'on';
    if (FALSE_WORDS.has(lowered)) return 'off';
    return lowered;
  }
  if (vartype === 'enum') return lowered;
  if (vartype === 'string') {
    return text.includes(',')
      ? text
          .split(',')
          .map((item) => item.trim())
          .join(',')
      : text;
  }

  const match = NUMERIC_PATTERN.exec(text);
  if (match === null) return text;
  const amount = Number(match[1]);
  const unit = match[2] ?? sourceUnit ?? '';
  const target = metadata?.unit ?? '';
  if (!unit && !target) return numericKey(amount);
  try {
    if (target) return numericKey(numericValueInSettingUnits(amount, unit || target, target));
    // No snapshot unit for this setting: compare in the smallest unit of the
    // kind the suffix names, so 1s and 1000ms still agree.
    const base = /^(us|ms|s|min|h|d)$/.test(unit) ? 'ms' : 'B';
    return numericKey(numericValueInSettingUnits(amount, unit, base));
  } catch {
    return text;
  }
}

/**
 * The settings this run calculated that the pasted configuration sets to a
 * different value. `matching` counts agreement, `missing` the calculated
 * settings the input does not mention, `unknown` the pasted names this tool
 * does not calculate.
 */
export function diffConfigurations(parameters, entries, metadata) {
  const rows = [];
  let matching = 0;
  let missing = 0;
  for (const [name, detail] of Object.entries(parameters)) {
    const entry = entries.get(name);
    if (entry === undefined) {
      missing += 1;
      continue;
    }
    const meta = metadata.get(name) ?? null;
    const calculated = normalizeSettingValue(detail.value, null, meta);
    const yours = normalizeSettingValue(entry.value, entry.unit, meta);
    if (calculated === yours) {
      matching += 1;
      continue;
    }
    rows.push({
      name,
      calculated: detail.value,
      yours: entry.unit ? `${entry.value} (${entry.unit})` : entry.value,
      apply_mode: detail.apply_mode,
      context: detail.context,
      source: detail.source,
    });
  }
  let unknown = 0;
  for (const name of entries.keys()) if (!(name in parameters)) unknown += 1;
  return { rows, matching, missing, unknown };
}
