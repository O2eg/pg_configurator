#!/usr/bin/env node
/**
 * Batched adapter used by the Python differential tests.
 *
 * The Python side is the oracle: it runs the calculation, records what it fed
 * the rule evaluator and what came back, and hands the whole batch here in one
 * file. One process per batch is deliberate — a process per case would cost
 * more than the comparison itself.
 *
 * This is test infrastructure. The shipped Node CLI is a separate entry point;
 * both are built on the same core so the harness exercises what is released.
 *
 *   node web/tools/parity-runner.mjs --expressions <batch.json> [--out <file>]
 *   node web/tools/parity-runner.mjs --defaults [--out <file>]
 */

import { readFileSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import { createEnums, createRuleCallables } from '../src/configurator.js';
import { makeConf, makeConfDefaults } from '../src/make-conf.js';
import { RuleEvaluator } from '../src/rule-eval.js';
import { PyNumber, pyFloat, pyInt } from '../src/units.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const DATA_DIR = join(HERE, '..', 'data');

function loadData(name) {
  return JSON.parse(readFileSync(join(DATA_DIR, name), 'utf8')).payload;
}

/** Rebuild a Python value from its tagged form. */
function decode(tagged, enums) {
  switch (tagged.k) {
    case 'none':
      return null;
    case 'bool':
    case 'str':
      return tagged.v;
    case 'int':
      return pyInt(tagged.v);
    case 'float':
      return pyFloat(tagged.v);
    case 'enum':
      return enums[tagged.enum][tagged.name];
    case 'list':
    case 'tuple':
      return tagged.items.map((item) => decode(item, enums));
    default:
      throw new Error(`Cannot decode a value tagged ${tagged.k}`);
  }
}

/** Tag a value the way the Python side tags it, so the two can be compared. */
function encode(value) {
  if (value === null || value === undefined) return { k: 'none' };
  if (typeof value === 'boolean') return { k: 'bool', v: value };
  if (typeof value === 'string') return { k: 'str', v: value };
  if (value instanceof PyNumber) {
    return value.isInt ? { k: 'int', v: value.value } : { k: 'float', v: value.value };
  }
  if (Array.isArray(value)) return { k: 'tuple', items: value.map(encode) };
  if (value && typeof value === 'object' && value.__enum__) {
    return { k: 'enum', enum: value.__enum__, name: value.name };
  }
  throw new Error(`Cannot encode ${String(value)}`);
}

function buildContext(caseData, enums) {
  const environment = {};
  for (const [name, tagged] of Object.entries(caseData.environment)) {
    environment[name] = decode(tagged, enums);
  }
  const runtime = createRuleCallables(environment, enums);

  const context = { ...runtime.context };
  for (const [name, tagged] of Object.entries(caseData.context)) {
    // Callables and enum classes are provided by the port itself; the oracle
    // marks them opaque rather than trying to serialize a function.
    if (tagged.k === 'opaque') {
      if (!(name in context)) {
        throw new Error(`The port does not provide the context binding ${name}`);
      }
      continue;
    }
    context[name] = decode(tagged, enums);
  }
  return { context, runtime };
}

function run(batch) {
  const rules = loadData('rules.json');
  const enums = createEnums(rules.enums);
  const results = [];

  for (const caseData of batch.cases) {
    const { context, runtime } = buildContext(caseData, enums);
    const evaluator = new RuleEvaluator(context, {
      allowedCallables: runtime.allowedCallables,
      allowedAttributeRoots: runtime.allowedAttributeRoots,
    });

    const values = [];
    for (const item of caseData.expressions) {
      const tree = rules.expressions[item.expr];
      if (tree === undefined) {
        values.push({ expr: item.expr, error: { type: 'MissingExpression', message: item.expr } });
        continue;
      }
      try {
        values.push({ expr: item.expr, actual: encode(evaluator.evaluate(tree)) });
      } catch (error) {
        values.push({
          expr: item.expr,
          error: { type: error.name ?? 'Error', message: error.message },
        });
      }
    }
    results.push({ label: caseData.label, values, contextNames: Object.keys(context).sort() });
  }
  return { results };
}

/** Encode a whole calculation result for comparison with the Python artifact. */
function runConfigurations(batch) {
  const rules = loadData('rules.json');
  const pgSettings = loadData('pg_settings.json');
  const enums = createEnums(rules.enums);
  const data = { rules, pgSettings, enums };

  const results = [];
  for (const caseData of batch.cases) {
    try {
      const result = makeConf(caseData.cpu_cores, caseData.ram_value, caseData.options, data);
      results.push({
        label: caseData.label,
        config: result.config,
        inputs: result.inputs,
        extensions: result.extensions,
        calculation: result.calculation,
        advisories: result.advisories,
        overrides: result.overrides,
        parameters: Object.fromEntries(
          Object.entries(result.parameters).map(([name, detail]) => [
            name,
            { ...detail, raw_value: encode(detail.raw_value) },
          ]),
        ),
      });
    } catch (error) {
      results.push({
        label: caseData.label,
        error: { type: error.name ?? 'Error', message: error.message },
      });
    }
  }
  return { results };
}

function main(argv) {
  let inputPath = null;
  let outputPath = null;
  let mode = null;
  for (let index = 0; index < argv.length; index += 1) {
    if (argv[index] === '--expressions') {
      inputPath = argv[index + 1];
      mode = 'expressions';
    }
    if (argv[index] === '--configurations') {
      inputPath = argv[index + 1];
      mode = 'configurations';
    }
    if (argv[index] === '--defaults') mode = 'defaults';
    if (argv[index] === '--out') outputPath = argv[index + 1];
  }
  if (mode !== 'defaults' && inputPath === null) {
    process.stderr.write(
      'parity-runner: --expressions, --configurations <file>, or --defaults is required\n',
    );
    return 2;
  }
  let output;
  if (mode === 'defaults') {
    output = JSON.stringify(makeConfDefaults());
  } else {
    const batch = JSON.parse(readFileSync(inputPath, 'utf8'));
    output = JSON.stringify(mode === 'configurations' ? runConfigurations(batch) : run(batch));
  }
  if (outputPath) {
    writeFileSync(outputPath, output);
  } else {
    process.stdout.write(output);
  }
  return 0;
}

process.exitCode = main(process.argv.slice(2));
