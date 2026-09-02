/**
 * Native tests for the Python-compatible numerics.
 *
 * Every expectation in `fixtures/python-numerics.json` was computed by Python
 * (see `tests/unit/test_web_numeric_fixtures.py`, which also fails when the
 * committed fixture drifts). Nothing here encodes a guess about what Python
 * does.
 *
 * These cover ground the differential suite cannot reach: no rule expression
 * calls `round` or `UnitConverter.size_to`, so a rounding or formatting defect
 * would pass the expression parity run untouched.
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import {
  add,
  cpuToCores,
  divide,
  multiply,
  numberOf,
  pyCeil,
  pyFloat,
  pyFloor,
  pyInt,
  pyMax,
  pyMin,
  pyRound,
  pyStr,
  PyNumber,
  sizeFrom,
  sizeTo,
  subtract,
  SYS_IEC,
  SYS_ISO,
  SYS_PG,
  SYS_STD,
} from '../src/units.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const fixtures = JSON.parse(
  readFileSync(join(HERE, 'fixtures', 'python-numerics.json'), 'utf8'),
);

const SYSTEMS = { sys_std: SYS_STD, sys_iec: SYS_IEC, sys_iso: SYS_ISO, sys_pg: SYS_PG };
const OPERATIONS = { add, sub: subtract, mul: multiply, div: divide };

function decode({ k, v }) {
  if (k === 'int') return pyInt(v);
  if (k === 'float') return pyFloat(v);
  if (k === 'bool') return v;
  if (k === 'str') return v;
  if (k === 'none') return null;
  throw new Error(`unknown fixture kind ${k}`);
}

function kindOf(value) {
  return value.isInt ? 'int' : 'float';
}

test('round matches Python, half to even on the binary value', () => {
  for (const { value, digits, expected } of fixtures.round) {
    const actual = pyRound(pyFloat(value), digits);
    assert.equal(
      numberOf(actual),
      expected,
      `round(${value}, ${digits}) should be ${expected}, got ${numberOf(actual)}`,
    );
  }
});

test('round keeps the argument type and returns an int without digits', () => {
  assert.equal(kindOf(pyRound(pyFloat(2.5), 0)), 'float');
  assert.equal(kindOf(pyRound(pyFloat(2.5))), 'int');
  assert.equal(kindOf(pyRound(pyInt(7), 2)), 'int');
  assert.equal(numberOf(pyRound(pyInt(7), 2)), 7);
});

test('float formatting matches Python repr', () => {
  for (const { value, expected } of fixtures.repr) {
    assert.equal(pyStr(pyFloat(value)), expected, `repr of ${expected}`);
  }
});

test('str matches Python for the scalar kinds a rule can produce', () => {
  for (const { value, expected } of fixtures.str_scalars) {
    assert.equal(pyStr(decode(value)), expected);
  }
});

test('size_to matches Python for every unit system', () => {
  for (const { value, system, expected } of fixtures.size_to) {
    assert.equal(sizeTo(value, SYSTEMS[system]), expected, `size_to(${value}, ${system})`);
  }
});

test('size_from matches Python, including its rejections', () => {
  for (const item of fixtures.size_from) {
    if ('error' in item) {
      assert.throws(
        () => sizeFrom(item.value, SYSTEMS[item.system]),
        (error) => error.name === 'ValueError' && error.message === item.error,
        `size_from(${JSON.stringify(item.value)}) should raise ${item.error}`,
      );
    } else {
      assert.equal(
        numberOf(sizeFrom(item.value, SYSTEMS[item.system])),
        item.expected,
        `size_from(${JSON.stringify(item.value)}, ${item.system})`,
      );
    }
  }
});

test('size_from returns an int, never a float', () => {
  assert.equal(kindOf(sizeFrom('1.5Gi', SYS_IEC)), 'int');
});

test('cpu parsing matches Python, including millicores and rejections', () => {
  for (const item of fixtures.cpu_to_ncores) {
    if ('error' in item) {
      assert.throws(
        () => cpuToCores(item.value),
        (error) => error.name === 'ValueError' && error.message === item.error,
      );
    } else {
      assert.equal(numberOf(cpuToCores(item.value)), item.expected, `cpu ${item.value}`);
    }
  }
});

test('arithmetic reproduces Python int/float promotion', () => {
  for (const { op, left, right, expected } of fixtures.arithmetic) {
    const actual = OPERATIONS[op](decode(left), decode(right));
    assert.equal(numberOf(actual), expected.v, `${left.v} ${op} ${right.v}`);
    assert.equal(kindOf(actual), expected.k, `${left.v} ${op} ${right.v} type`);
  }
});

test('max and min return an operand, preserving its type', () => {
  for (const { values, max, min } of fixtures.extremes) {
    const decoded = values.map(decode);
    const gotMax = pyMax(...decoded);
    const gotMin = pyMin(...decoded);
    assert.equal(numberOf(gotMax), max.v);
    assert.equal(kindOf(gotMax), max.k, `max type for ${JSON.stringify(values)}`);
    assert.equal(numberOf(gotMin), min.v);
    assert.equal(kindOf(gotMin), min.k, `min type for ${JSON.stringify(values)}`);
  }
});

test('ceil and floor return ints', () => {
  for (const { value, ceil, floor } of fixtures.ceil_floor) {
    assert.equal(numberOf(pyCeil(pyFloat(value))), ceil);
    assert.equal(numberOf(pyFloor(pyFloat(value))), floor);
    assert.equal(kindOf(pyCeil(pyFloat(value))), 'int');
    assert.equal(kindOf(pyFloor(pyFloat(value))), 'int');
  }
});

test('division by zero raises the Python exception identity', () => {
  assert.throws(
    () => divide(pyInt(1), pyInt(0)),
    (error) => error.name === 'ZeroDivisionError' && error.message === 'division by zero',
  );
});

test('int() truncates toward zero the way Python does', () => {
  assert.equal(numberOf(pyInt(4.9)), 4);
  assert.equal(numberOf(pyInt(-4.9)), -4);
  assert.equal(numberOf(pyInt(-0.5)), 0);
});

test('a non-integral value cannot be tagged as an int', () => {
  assert.throws(() => new PyNumber(4.5, 'int'), TypeError);
  assert.throws(() => new PyNumber(Number.NaN, 'float'), TypeError);
  assert.throws(() => new PyNumber(Infinity, 'float'), TypeError);
});
