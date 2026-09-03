#!/usr/bin/env node
/**
 * pg-configurator, JavaScript build.
 *
 * The same calculation and the same command line as the Python reference, on
 * the same generated rule data. It exists so the port can be exercised the way
 * a user actually drives the tool, and so the differential tests can compare on
 * the command-line boundary rather than on an internal call.
 *
 * What this build deliberately does not do:
 *
 *   - the orchestration contract (`--machine`, `--capabilities`, `--request-id`,
 *     `--validate-input`). pg_play's authoritative artifact comes from Python,
 *     and a second implementation announcing the same component identity would
 *     be a lie to an orchestrator. The options are recognized and refused with
 *     an explanation rather than silently ignored;
 *   - `--settings-history` and `--specific-setting-history`, which need
 *     snapshot columns the web data layer does not ship.
 *
 * The JSON artifact reports `generator.name = "pg-configurator-js"` and carries
 * no `artifact_hash`: Python's canonical serialization is the authority for
 * that, and claiming it here would be unearned.
 */

import { readFileSync, writeFileSync, existsSync, renameSync, statSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { cpus, hostname, totalmem } from 'node:os';

import { argumentsFromInputJson, CliError, parseArgs } from '../src/cli-args.js';
import { createEnums } from '../src/configurator.js';
import { makeConf, unquotePostgresqlConfValue } from '../src/make-conf.js';
import { sizeTo, SYS_IEC } from '../src/units.js';
// The rendered file is the library's, so the CLI and an embedder cannot drift.
import { renderConf } from '../index.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const DATA_DIR = join(HERE, '..', 'data');
const PROG = 'pg-configurator';

// Exit codes are the reference implementation's.
const EXIT = { success: 0, validation_error: 2, unsupported: 4 };

const UNSUPPORTED = {
  machine: '--machine',
  capabilities: '--capabilities',
  request_id: '--request-id',
  validate_input: '--validate-input',
  settings_history: '--settings-history',
  specific_setting_history: '--specific-setting-history',
};

function loadData(name) {
  return JSON.parse(readFileSync(join(DATA_DIR, name), 'utf8'));
}

function hostDefaults() {
  return {
    // psutil.cpu_count() and psutil.virtual_memory().total on the Python side.
    // A test asserts the two agree rather than trusting that they do.
    db_cpu: String(cpus().length),
    db_ram: sizeTo(totalmem(), SYS_IEC),
  };
}

function readJsonArgument(path) {
  const text = path === '-' ? readFileSync(0, 'utf8') : readFileSync(path, 'utf8');
  return JSON.parse(text);
}

/** `json.dumps(value, indent=2, sort_keys=True)`. */
function stableStringify(value) {
  const sortKeys = (item) => {
    if (Array.isArray(item)) return item.map(sortKeys);
    if (item && typeof item === 'object') {
      return Object.fromEntries(
        Object.keys(item)
          .sort()
          .map((key) => [key, sortKeys(item[key])]),
      );
    }
    return item;
  };
  return JSON.stringify(sortKeys(value), null, 2);
}

function patroniDocument(config) {
  const parameters = {};
  for (const [name, value] of Object.entries(config)) {
    parameters[name] = unquotePostgresqlConfValue(value);
  }
  parameters.max_replication_slots = String(
    Math.max(4, Math.trunc(Number(parameters.max_replication_slots))),
  );
  return { postgresql: { parameters } };
}

// A preview, not a `pg_configurator/v2` artifact. That schema carries an
// `artifact_hash` over the canonical JSON encoding of the Python reference,
// down to how a float that happens to be integral is spelled; this build emits
// the same content but does not reproduce those bytes, so it must not claim
// that name. Everything else is identical, which a parity test checks.
function buildArtifact(result, version) {
  return {
    schema_version: 'pg_configurator/preview-v1',
    kind: 'PostgreSQLConfigurationPreview',
    generator: { name: 'pg-configurator-js', version },
    generated_at: new Date().toISOString().replace(/\.\d{3}Z$/, 'Z'),
    inputs: result.inputs,
    extensions: result.extensions,
    calculation: result.calculation,
    parameters: Object.fromEntries(
      Object.entries(result.parameters).map(([name, detail]) => [
        name,
        { ...detail, raw_value: rawValueForJson(detail.raw_value) },
      ]),
    ),
    overrides: result.overrides,
    advisories: result.advisories,
    postgresql_conf: result.config,
  };
}

function rawValueForJson(value) {
  if (value && typeof value === 'object' && 'kind' in value && 'value' in value) {
    return value.value;
  }
  return value;
}

function writeOutput(text, outputFileName) {
  if (!outputFileName) {
    process.stdout.write(text);
    return;
  }
  if (existsSync(outputFileName) && statSync(outputFileName).isDirectory()) {
    throw new CliError('output_file_name points to a directory');
  }
  if (existsSync(outputFileName)) {
    const now = new Date();
    const pad = (value, width = 2) => String(value).padStart(width, '0');
    const stamp =
      `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}T` +
      `${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}` +
      `${pad(now.getMilliseconds(), 3)}000`;
    renameSync(outputFileName, `${outputFileName}.${stamp}.bak`);
  }
  writeFileSync(outputFileName, text, 'utf8');
}

export function run(argv) {
  const schemaDocument = loadData('input-schema.json');
  const schema = schemaDocument.payload;
  const version = schemaDocument.package_version;

  const expanded = argumentsFromInputJson(argv, schema, readJsonArgument);
  const { values, supplied } = parseArgs(expanded, schema, { hostDefaults: hostDefaults() });

  for (const [dest, option] of Object.entries(UNSUPPORTED)) {
    if (supplied.has(dest)) {
      throw new CliError(
        `${option} is not implemented in the JavaScript build; use the Python pg-configurator`,
        EXIT.unsupported,
      );
    }
  }

  if (values.version) {
    process.stdout.write(`${PROG} ${version}\n`);
    return EXIT.success;
  }

  const rules = loadData('rules.json').payload;
  const pgSettings = loadData('pg_settings.json').payload;
  const enums = createEnums(rules.enums);

  const options = {};
  for (const option of schema.options) {
    if (option.role !== 'calculation') continue;
    options[option.make_conf_parameter] = values[option.dest];
  }
  const cpuCores = options.cpu_cores;
  const ramValue = options.ram_value;
  delete options.cpu_cores;
  delete options.ram_value;

  const result = makeConf(cpuCores, ramValue, options, { rules, pgSettings, enums });

  const outputFormat = values.output_format;
  let output;
  if (outputFormat === 'conf') {
    output = renderConf(result, { version, host: hostname() });
  } else if (outputFormat === 'json') {
    output = `${stableStringify(buildArtifact(result, version))}\n`;
  } else {
    output = `${stableStringify(patroniDocument(result.config))}\n`;
  }

  writeOutput(output, values.output_file_name);
  return EXIT.success;
}

function main(argv) {
  try {
    return run(argv);
  } catch (error) {
    if (error instanceof CliError) {
      process.stderr.write(`${PROG}: error: ${error.message}\n`);
      return error.exitCode;
    }
    if (error && (error.name === 'ValueError' || error.name === 'RuleEvaluationError')) {
      process.stderr.write(`${PROG}: error: ${error.message}\n`);
      return EXIT.validation_error;
    }
    throw error;
  }
}

process.exitCode = main(process.argv.slice(2));
