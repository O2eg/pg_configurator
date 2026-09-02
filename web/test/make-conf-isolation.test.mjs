/**
 * One calculation must not be able to spoil the next.
 *
 * The bundled rules are loaded once and shared by every call. Python hands the
 * caller copies (`list(...)`, `dict(sorted(...))`) and keeps the module-level
 * rule data to itself; the port has to do the same, or a caller who edits a
 * result — or merely sorts an array in place — silently rewrites the rules for
 * the rest of the process.
 *
 * Two directions are checked. Nothing the calculation returns may alias the
 * rule data, and the calculation itself may not write into it: the second test
 * freezes the input all the way down, which turns any write into a throw.
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import { makeConf } from '../src/make-conf.js';
import { createEnums } from '../src/configurator.js';

const DATA = join(dirname(fileURLToPath(import.meta.url)), '..', 'data');
const load = (name) => JSON.parse(readFileSync(join(DATA, name), 'utf8')).payload;

function freshData() {
  const rules = load('rules.json');
  return { rules, pgSettings: load('pg_settings.json'), enums: createEnums(rules.enums) };
}

function deepFreeze(value, seen = new WeakSet()) {
  if (!value || typeof value !== 'object' || seen.has(value)) return value;
  seen.add(value);
  Object.freeze(value);
  for (const key of Object.keys(value)) deepFreeze(value[key], seen);
  return value;
}

test('a result cannot be edited into breaking the next call', () => {
  const data = freshData();
  const first = makeConf('8', '16Gi', { pg_version: '18' }, data);

  for (const extension of first.extensions) {
    extension.supported_versions.splice(0);
  }
  first.inputs.profiles.push('nonexistent_profile');

  const second = makeConf('8', '16Gi', { pg_version: '18' }, data);
  assert.deepEqual(second.config, makeConf('8', '16Gi', { pg_version: '18' }, freshData()).config);
  assert.ok(second.extensions.every((extension) => extension.supported_versions.length > 0));
  assert.deepEqual([], second.inputs.profiles);
});

test('the calculation writes nothing into the rules it was given', () => {
  const data = freshData();
  deepFreeze(data.rules);
  deepFreeze(data.pgSettings);

  // Profiles are the interesting case: they extend the rule set for the run.
  const result = makeConf('8', '16Gi', { pg_version: '18', conf_profiles: 'profile_1c' }, data);
  assert.equal('off', result.config.ssl);
});

test('repeated calls with the same inputs give the same answer', () => {
  const data = freshData();
  const options = { pg_version: '18', conf_profiles: 'profile_backend_common,profile_backend_perf' };
  const first = makeConf('8', '16Gi', options, data);
  const plain = makeConf('8', '16Gi', { pg_version: '18' }, data);
  const third = makeConf('8', '16Gi', options, data);

  assert.deepEqual(first.config, third.config);
  assert.deepEqual(first.advisories, third.advisories);
  assert.notDeepEqual(plain.config, first.config);
});
