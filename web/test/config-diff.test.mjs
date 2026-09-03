/**
 * The Diff tab's reader and comparator, on the shapes a DBA actually pastes.
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import { loadSettingMetadata } from '../src/make-conf.js';
import {
  detectFormat,
  diffConfigurations,
  normalizeSettingValue,
  parseUserConfig,
  unquoteConfValue,
} from '../src/config-diff.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const SNAPSHOT = JSON.parse(readFileSync(join(HERE, '..', 'data', 'pg_settings.json'), 'utf8'));
const METADATA = loadSettingMetadata(SNAPSHOT.payload, '18');

const entriesOf = (parsed) => Object.fromEntries([...parsed.entries].map(([k, v]) => [k, v.value]));

test('postgresql.conf is read the way the server reads it', () => {
  const parsed = parseUserConfig(
    [
      '# a comment line',
      "shared_buffers = 4GB   # trailing comment",
      "work_mem = '64MB'",
      "log_line_prefix = '%m [%p] # not a comment'",
      "search_path = 'a''b, public'",
      'max_connections 200',
      "include 'extra.conf'",
      'work_mem = 128MB',
      '',
    ].join('\n'),
  );
  assert.equal(parsed.format, 'conf');
  assert.deepEqual(entriesOf(parsed), {
    shared_buffers: '4GB',
    work_mem: '128MB',
    log_line_prefix: '%m [%p] # not a comment',
    search_path: "a'b, public",
    max_connections: '200',
  });
  assert.equal(parsed.skipped, 1, 'the include directive is skipped, not read as a setting');
});

test('quoted conf values lose their quotes and keep their escapes', () => {
  assert.equal(unquoteConfValue("'plain'"), 'plain');
  assert.equal(unquoteConfValue("'it''s'"), "it's");
  assert.equal(unquoteConfValue("'it\\'s'"), "it's");
  assert.equal(unquoteConfValue('bare'), 'bare');
});

test('a pg_settings export with a header row uses name, setting and unit', () => {
  const parsed = parseUserConfig(
    [
      'name,setting,unit,category',
      'shared_buffers,524288,8kB,Resource Usage / Memory',
      '"shared_preload_libraries","pg_stat_statements,auto_explain",,"Client Connection Defaults"',
      'jit,on,,Query Tuning',
    ].join('\n'),
  );
  assert.equal(parsed.format, 'csv');
  assert.equal(parsed.header, true);
  assert.deepEqual([...parsed.entries], [
    ['shared_buffers', { value: '524288', unit: '8kB' }],
    ['shared_preload_libraries', { value: 'pg_stat_statements,auto_explain', unit: null }],
    ['jit', { value: 'on', unit: null }],
  ]);
});

test('an export without a header is name, value and an optional unit', () => {
  const parsed = parseUserConfig('shared_buffers;524288;8kB\nwork_mem;65536\njit;off;Query Tuning');
  assert.equal(parsed.format, 'csv');
  assert.equal(parsed.header, false);
  assert.equal(parsed.delimiter, ';');
  assert.deepEqual([...parsed.entries], [
    ['shared_buffers', { value: '524288', unit: '8kB' }],
    ['work_mem', { value: '65536', unit: null }],
    ['jit', { value: 'off', unit: null }],
  ]);
});

test("psql's aligned output reads as a pipe-delimited export", () => {
  const parsed = parseUserConfig(
    [
      '      name       | setting ',
      '-----------------+---------',
      ' shared_buffers  | 524288',
      ' work_mem        | 65536',
      '(2 rows)',
    ].join('\n'),
  );
  assert.equal(parsed.format, 'csv');
  assert.equal(parsed.header, true);
  assert.deepEqual(entriesOf(parsed), { shared_buffers: '524288', work_mem: '65536' });
  assert.equal(parsed.skipped, 2, 'the rule and the row count are skipped');
});

test('empty and unreadable text are named as such', () => {
  assert.equal(parseUserConfig('').format, 'empty');
  assert.equal(parseUserConfig('   \n\n').format, 'empty');
  assert.equal(parseUserConfig('just some prose without structure').format, 'unknown');
  assert.equal(detectFormat('a\tb\nc\td').delimiter, '\t');
});

test('values are compared through the snapshot unit, not as text', () => {
  const shared = METADATA.get('shared_buffers');
  const same = new Set(
    ['8GB', '8192MB', '8388608kB', "'8GB'", '1048576'].map((value) =>
      normalizeSettingValue(value, null, shared),
    ),
  );
  assert.equal(same.size, 1, '8GB has one key however it is spelled');
  assert.equal(normalizeSettingValue('1048576', '8kB', shared), normalizeSettingValue('8GB', null, shared));
  assert.notEqual(normalizeSettingValue('4GB', null, shared), normalizeSettingValue('8GB', null, shared));

  const timeout = METADATA.get('checkpoint_timeout');
  assert.equal(normalizeSettingValue('15min', null, timeout), normalizeSettingValue('900', null, timeout));
  assert.equal(normalizeSettingValue('900s', null, timeout), normalizeSettingValue('900', 's', timeout));

  const jit = METADATA.get('jit');
  assert.equal(normalizeSettingValue('true', null, jit), normalizeSettingValue('on', null, jit));
  assert.equal(normalizeSettingValue('0', null, jit), 'off');

  const commit = METADATA.get('synchronous_commit');
  assert.equal(normalizeSettingValue('REMOTE_APPLY', null, commit), 'remote_apply');

  const libraries = METADATA.get('shared_preload_libraries');
  assert.equal(
    normalizeSettingValue("'pg_stat_statements, auto_explain'", null, libraries),
    normalizeSettingValue('pg_stat_statements,auto_explain', null, libraries),
  );

  const cost = METADATA.get('random_page_cost');
  assert.equal(normalizeSettingValue('1.10', null, cost), normalizeSettingValue('1.1', null, cost));

  // A setting the snapshot does not know still compares time and size units.
  assert.equal(normalizeSettingValue('1s', null, null), normalizeSettingValue('1000ms', null, null));
  assert.equal(normalizeSettingValue('not a number', null, shared), 'not a number');
});

test('the diff lists only the calculated settings the input sets differently', () => {
  const parameters = {
    shared_buffers: { value: '8GB', apply_mode: 'restart', context: 'postmaster', source: 'base' },
    work_mem: { value: '64MB', apply_mode: 'reload', context: 'user', source: 'base' },
    jit: { value: 'on', apply_mode: 'reload', context: 'user', source: 'base' },
    checkpoint_timeout: { value: '15min', apply_mode: 'reload', context: 'sighup', source: 'base' },
  };
  const parsed = parseUserConfig(
    ['name,setting,unit', 'shared_buffers,1048576,8kB', 'work_mem,4096,kB', 'jit,off,', 'port,5433,'].join(
      '\n',
    ),
  );
  const diff = diffConfigurations(parameters, parsed.entries, METADATA);
  assert.deepEqual(
    diff.rows.map((row) => [row.name, row.calculated, row.yours, row.apply_mode]),
    [
      ['work_mem', '64MB', '4096 (kB)', 'reload'],
      ['jit', 'on', 'off', 'reload'],
    ],
  );
  assert.equal(diff.matching, 1);
  assert.equal(diff.missing, 1, 'checkpoint_timeout is not in the input');
  assert.equal(diff.unknown, 1, 'port is not something this tool calculates');
});
