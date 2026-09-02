/**
 * argparse-compatible argument parsing, driven by the generated input schema.
 *
 * The option table is not retyped here: it comes from `web/data/input-schema.json`,
 * which the exporter builds from the Python parser. A new option therefore
 * appears in the Node CLI automatically, and one that the port cannot handle
 * fails the export instead of silently disappearing.
 *
 * argparse behaviours reproduced on purpose, because a user relies on them:
 *
 *   - unambiguous long-option abbreviation (`--db-c` means `--db-cpu`);
 *   - `--name value` and `--name=value`;
 *   - `BooleanOptionalAction` (`--common-conf` / `--no-common-conf`);
 *   - option aliases (`--out` / `--output-file-name`);
 *   - `--input-json` expansion ahead of the command line, so an explicit
 *     option still wins;
 *   - the error texts, verbatim, including how an aliased option is named.
 *
 * Errors carry exit code 2, the same as argparse.
 */

import { PyValueError } from './units.js';

export class CliError extends Error {
  constructor(message, exitCode = 2) {
    super(message);
    this.name = 'CliError';
    this.exitCode = exitCode;
  }
}

const BOOLEAN_TRUE = ['true', '1', 'yes', 'on'];
const BOOLEAN_FALSE = ['false', '0', 'no', 'off'];

/** Python's `repr` for the scalars that appear in argparse messages. */
export function pyRepr(value) {
  if (typeof value !== 'string') return String(value);
  const escaped = value.replace(/\\/g, '\\\\').replace(/\n/g, '\\n');
  return escaped.includes("'") && !escaped.includes('"')
    ? `"${escaped}"`
    : `'${escaped.replace(/'/g, "\\'")}'`;
}

/** `argparse._get_action_name`: every option string, joined by a slash. */
function actionName(option) {
  return option.option_strings.join('/');
}

function parseBool(value, option) {
  if (typeof value === 'boolean') return value;
  const normalized = String(value).trim().toLowerCase();
  if (BOOLEAN_TRUE.includes(normalized)) return true;
  if (BOOLEAN_FALSE.includes(normalized)) return false;
  throw new CliError(
    `argument ${actionName(option)}: expected one of: true, false, 1, 0, yes, no, on, off`,
  );
}

function coerce(rawValue, option) {
  const typeName = option.type;
  if (typeName === null || typeName === 'str') return rawValue;
  if (typeName === 'parse_bool') return parseBool(rawValue, option);
  if (typeName === 'int') {
    if (!/^\s*[+-]?\d+\s*$/.test(rawValue)) {
      throw new CliError(
        `argument ${actionName(option)}: invalid int value: ${pyRepr(rawValue)}`,
      );
    }
    return Number(rawValue.trim());
  }
  if (typeName === 'float') {
    const parsed = Number(rawValue);
    if (rawValue.trim() === '' || Number.isNaN(parsed)) {
      throw new CliError(
        `argument ${actionName(option)}: invalid float value: ${pyRepr(rawValue)}`,
      );
    }
    return parsed;
  }
  // An enum type: argparse calls the class, which raises before the choices
  // check ever runs, so the message names the type rather than the choices.
  if (option.choices !== null && !option.choices.includes(rawValue)) {
    throw new CliError(
      `argument ${actionName(option)}: invalid ${typeName} value: ${pyRepr(rawValue)}`,
    );
  }
  return rawValue;
}

function checkChoices(value, option) {
  if (option.choices === null) return value;
  if (!option.choices.includes(value)) {
    const rendered = option.choices.map(pyRepr).join(', ');
    throw new CliError(
      `argument ${actionName(option)}: invalid choice: ${pyRepr(value)} (choose from ${rendered})`,
    );
  }
  return value;
}

function buildIndex(schema) {
  const byOption = new Map();
  const negatives = new Map();
  for (const option of schema.options) {
    for (const optionString of option.option_strings) {
      byOption.set(optionString, option);
      if (option.action === 'boolean_optional' && optionString.startsWith('--')) {
        negatives.set(`--no-${optionString.slice(2)}`, option);
      }
    }
  }
  return { byOption, negatives };
}

/**
 * `argparse._parse_optional`, reduced to the one question the value loop asks.
 *
 * A token that could be an option is never handed to the previous option as
 * its value, whether or not this parser knows the name: argparse marks any such
 * token `O` when it builds the pattern it matches arguments against, and an
 * option still waiting for a value then reports that it got none. What escapes
 * are the tokens that cannot be an option at all — a lone `-`, a negative
 * number (this parser declares no option that looks like one), and anything
 * with a space in it.
 */
function looksLikeAnOption(token, index) {
  if (!token.startsWith('-')) return false;
  if (index.byOption.has(token) || index.negatives.has(token)) return true;
  if (token.length === 1) return false;
  if (/^-\d+$|^-\d*\.\d+$/.test(token)) return false;
  if (token.includes(' ')) return false;
  return true;
}

/**
 * Resolve one long option, accepting an unambiguous abbreviation.
 *
 * `argument` is passed whole (including any `=value`) because argparse quotes
 * it that way in the ambiguity message.
 */
function resolve(name, argument, index, allowAbbrev) {
  if (index.byOption.has(name)) return { option: index.byOption.get(name), negated: false };
  if (index.negatives.has(name)) return { option: index.negatives.get(name), negated: true };
  if (!allowAbbrev) return null;

  const candidates = [];
  for (const [optionString, option] of index.byOption) {
    if (optionString.startsWith(name)) candidates.push([optionString, option]);
  }
  for (const [optionString, option] of index.negatives) {
    if (optionString.startsWith(name)) candidates.push([optionString, option]);
  }
  if (candidates.length === 0) return null;
  const distinct = new Set(candidates.map(([optionString]) => optionString));
  if (distinct.size > 1) {
    throw new CliError(
      `ambiguous option: ${argument} could match ${[...distinct].join(', ')}`,
    );
  }
  const [optionString, option] = candidates[0];
  return { option, negated: optionString.startsWith('--no-') && index.negatives.has(optionString) };
}

/**
 * Expand `--input-json` into leading arguments.
 *
 * Mirrors `_arguments_from_input_json`: generated arguments come first so an
 * explicit command-line option still wins.
 */
export function argumentsFromInputJson(argv, schema, readJson) {
  let inputPath = null;
  for (let index = 0; index < argv.length; index += 1) {
    if (argv[index].startsWith('--input-json=')) {
      inputPath = argv[index].slice('--input-json='.length);
      break;
    }
    if (argv[index] === '--input-json') {
      if (index + 1 >= argv.length) {
        throw new PyValueError('--input-json requires a file path or - for stdin');
      }
      inputPath = argv[index + 1];
      break;
    }
  }
  if (inputPath === null) return argv;

  let document = readJson(inputPath);
  if (document === null || typeof document !== 'object' || Array.isArray(document)) {
    throw new PyValueError('--input-json must contain a JSON object');
  }
  if ('inputs' in document) {
    if (document.schema_version !== 'pg_configurator/input-v1') {
      throw new PyValueError(
        'JSON input with an inputs field requires schema_version=pg_configurator/input-v1',
      );
    }
    document = document.inputs;
    if (document === null || typeof document !== 'object' || Array.isArray(document)) {
      throw new PyValueError('input JSON field inputs must be an object');
    }
  }

  const actions = new Map();
  for (const option of schema.options) {
    if (option.option_strings.length && option.role !== 'orchestration') {
      actions.set(option.dest, option);
    }
  }
  const normalized = new Map();
  for (const [key, value] of Object.entries(document)) {
    normalized.set(String(key).replace(/-/g, '_'), value);
  }
  const unknown = [...normalized.keys()].filter((key) => !actions.has(key)).sort();
  if (unknown.length) {
    throw new PyValueError(`unknown input JSON field(s): ${unknown.join(', ')}`);
  }

  const generated = [];
  for (const destination of [...normalized.keys()].sort()) {
    const value = normalized.get(destination);
    if (value === null) continue;
    const option = actions.get(destination);
    const positive =
      option.option_strings.find(
        (item) => item.startsWith('--') && !item.startsWith('--no-'),
      ) ?? option.option_strings[0];
    if (option.action === 'boolean_optional') {
      if (typeof value !== 'boolean') {
        throw new PyValueError(`input JSON field ${destination} must be boolean`);
      }
      generated.push(value ? positive : `--no-${positive.slice(2)}`);
    } else if (typeof value === 'boolean') {
      generated.push(positive, value ? 'true' : 'false');
    } else if (typeof value === 'object') {
      throw new PyValueError(`input JSON field ${destination} must be a scalar value`);
    } else {
      generated.push(positive, String(value));
    }
  }
  return [...generated, ...argv];
}

/**
 * Parse a command line into option values.
 *
 * `hostDefaults` supplies the values argparse reads from the host through
 * psutil; the export deliberately leaves them null so the generated data stays
 * machine-independent.
 */
export function parseArgs(argv, schema, { hostDefaults = {} } = {}) {
  const index = buildIndex(schema);
  const allowAbbrev = schema.parser.allow_abbrev;
  const values = {};
  const supplied = new Set();
  const unrecognized = [];

  for (const option of schema.options) {
    values[option.dest] =
      option.default_source === 'host' ? (hostDefaults[option.dest] ?? null) : option.default;
  }

  for (let position = 0; position < argv.length; position += 1) {
    const argument = argv[position];
    if (!argument.startsWith('--')) {
      unrecognized.push(argument);
      continue;
    }
    const equals = argument.indexOf('=');
    const name = equals === -1 ? argument : argument.slice(0, equals);
    const inlineValue = equals === -1 ? null : argument.slice(equals + 1);

    const resolved = resolve(name, argument, index, allowAbbrev);
    if (resolved === null) {
      unrecognized.push(argument);
      continue;
    }
    const { option, negated } = resolved;

    if (option.action === 'store_true' || option.action === 'boolean_optional') {
      // A flag takes no argument, so argparse refuses the `=value` form rather
      // than reading it: `--common-conf=false` would otherwise turn the option
      // on, which is the opposite of what it says.
      if (inlineValue !== null) {
        throw new CliError(
          `argument ${actionName(option)}: ignored explicit argument ${pyRepr(inlineValue)}`,
        );
      }
      values[option.dest] = option.action === 'store_true' ? true : !negated;
    } else {
      let rawValue = inlineValue;
      if (rawValue === null) {
        if (position + 1 >= argv.length || looksLikeAnOption(argv[position + 1], index)) {
          throw new CliError(`argument ${actionName(option)}: expected one argument`);
        }
        position += 1;
        rawValue = argv[position];
      }
      values[option.dest] = checkChoices(coerce(rawValue, option), option);
    }
    supplied.add(option.dest);
  }

  if (unrecognized.length) {
    throw new CliError(`unrecognized arguments: ${unrecognized.join(' ')}`);
  }
  return { values, supplied };
}
