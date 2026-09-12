import test from 'node:test';
import assert from 'node:assert/strict';
import { formatInputValue } from '../src/render.js';

test('every size input uses compact IEC notation for raw report bytes', () => {
  const cases = [
    ['db_ram', '66806628352', '62.22Gi'],
    ['db_size', String(923 * 1024 ** 3), '923Gi'],
    ['db_size', '1493076418', '1.39Gi'],
    ['reserved_system_ram', String(256 * 1024 ** 2), '256Mi'],
    ['peak_wal_rate', String(2.25 * 1024 ** 2), '2.25Mi'],
    ['peak_wal_rate', '101', '101B'],
    ['wal_disk_budget', String(2 * 1024 ** 3), '2Gi'],
    ['wal_segment_size', '16777216', '16Mi'],
    ['wal_segment_size', '1048576', '1Mi'],
    ['db_ram', '16Gi', '16Gi'],
    ['db_ram', '1024Gi', '1Ti'],
    ['db_ram', '0', '0B'],
    ['db_ram', 16 * 1024 ** 3, '16Gi'],
    ['db_ram', '1048576B', '1Mi'],
  ];
  for (const [dest, raw, expected] of cases) {
    assert.equal(formatInputValue(dest, raw), expected, `${dest}=${raw}`);
  }
});

test('formatting preserves unset, invalid and non-size inputs', () => {
  for (const value of [null, undefined, '']) assert.equal(formatInputValue('db_size', value), '');
  for (const value of ['32Zi', '1..2Gi', '-1']) assert.equal(formatInputValue('db_ram', value), value);
  for (const [dest, value] of [['db_cpu', '1.5'], ['max_conns', 96],
    ['reserved_ram_percent', 10], ['shared_buffers_part', 0.25], ['replica_count', 2],
    ['replica_outage_tolerance', 3600], ['pitr_enabled', false], ['db_disk_type', 'NVME']]) {
    assert.equal(formatInputValue(dest, value), String(value));
  }
});
