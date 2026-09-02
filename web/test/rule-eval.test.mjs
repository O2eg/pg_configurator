/**
 * Native tests for the exported-AST walker.
 *
 * Parsing happens in Python, so these do not test syntax. They test the
 * semantics the two languages do not share: `and`/`or` return an operand
 * rather than a boolean, comparisons chain, membership works on strings and
 * sequences, and the permitted call/attribute surface is enforced at run time
 * the way `RuleEvaluator` enforces it.
 *
 * The trees are the ones the exporter produces, read from the generated data
 * where possible so the shapes stay real.
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import { makeEnum, RuleEvaluationError, RuleEvaluator } from '../src/rule-eval.js';
import { numberOf, pyFloat, pyInt, pyStr } from '../src/units.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const rules = JSON.parse(
  readFileSync(join(HERE, '..', 'data', 'rules.json'), 'utf8'),
).payload;

const DutyDB = makeEnum('DutyDB', rules.enums.DutyDB);

const constant = (kind, value) => ({ t: 'Const', k: kind, v: value });
const name = (id) => ({ t: 'Name', id });

function evaluate(tree, context = {}, options = {}) {
  const evaluator = new RuleEvaluator(context, {
    allowedCallables: options.allowedCallables ?? [],
    allowedAttributeRoots: options.allowedAttributeRoots ?? [],
  });
  return evaluator.evaluate(tree);
}

test('constants keep the Python type the exporter recorded', () => {
  assert.equal(pyStr(evaluate(constant('int', 8))), '8');
  assert.equal(pyStr(evaluate(constant('float', 8))), '8.0');
  assert.equal(evaluate(constant('bool', true)), true);
  assert.equal(evaluate(constant('str', 'on')), 'on');
  assert.equal(evaluate(constant('none', null)), null);
});

test('an unknown name is reported the way Python reports it', () => {
  assert.throws(
    () => evaluate(name('missing')),
    (error) => error instanceof RuleEvaluationError && error.message === 'Unknown rule name: missing',
  );
});

test('and and or return an operand, not a boolean', () => {
  const or = { t: 'BoolOp', op: 'Or', values: [constant('str', ''), constant('str', 'fallback')] };
  assert.equal(evaluate(or), 'fallback');

  const and = { t: 'BoolOp', op: 'And', values: [constant('int', 5), constant('str', 'last')] };
  assert.equal(evaluate(and), 'last');

  const shortCircuit = {
    t: 'BoolOp',
    op: 'And',
    values: [constant('int', 0), name('never_evaluated')],
  };
  assert.equal(numberOf(evaluate(shortCircuit)), 0);
});

test('truthiness follows Python for every value a rule can hold', () => {
  const truthy = (node) =>
    evaluate({ t: 'IfExp', test: node, body: constant('str', 'yes'), orelse: constant('str', 'no') });

  assert.equal(truthy(constant('int', 0)), 'no');
  assert.equal(truthy(constant('float', 0)), 'no');
  assert.equal(truthy(constant('str', '')), 'no');
  assert.equal(truthy(constant('none', null)), 'no');
  assert.equal(truthy(constant('bool', false)), 'no');
  assert.equal(truthy({ t: 'List', elements: [] }), 'no');
  assert.equal(truthy(constant('float', 0.0001)), 'yes');
  assert.equal(truthy(constant('str', '0')), 'yes');
});

test('comparisons chain and short-circuit', () => {
  const chained = {
    t: 'Compare',
    left: constant('int', 1),
    ops: ['Lt', 'Lt'],
    comparators: [constant('int', 2), constant('int', 3)],
  };
  assert.equal(evaluate(chained), true);

  const broken = {
    t: 'Compare',
    left: constant('int', 1),
    ops: ['Lt', 'Lt'],
    comparators: [constant('int', 2), constant('int', 2)],
  };
  assert.equal(evaluate(broken), false);
});

test('int and float compare by value, as Python does', () => {
  const equal = {
    t: 'Compare',
    left: constant('int', 8),
    ops: ['Eq'],
    comparators: [constant('float', 8)],
  };
  assert.equal(evaluate(equal), true);
});

test('enum members compare by identity through an allowed root', () => {
  const tree = {
    t: 'Compare',
    left: name('duty_db'),
    ops: ['Eq'],
    comparators: [{ t: 'Attribute', value: name('DutyDB'), attr: 'FINANCIAL' }],
  };
  const context = { duty_db: DutyDB.FINANCIAL, DutyDB };
  assert.equal(evaluate(tree, context, { allowedAttributeRoots: [DutyDB] }), true);

  const other = { duty_db: DutyDB.OLTP, DutyDB };
  assert.equal(evaluate(tree, other, { allowedAttributeRoots: [DutyDB] }), false);
});

test('membership works on sequences and strings', () => {
  const inList = {
    t: 'Compare',
    left: constant('str', 'b'),
    ops: ['In'],
    comparators: [
      { t: 'List', elements: [constant('str', 'a'), constant('str', 'b')] },
    ],
  };
  assert.equal(evaluate(inList), true);

  const notInText = {
    t: 'Compare',
    left: constant('str', 'z'),
    ops: ['NotIn'],
    comparators: [constant('str', 'abc')],
  };
  assert.equal(evaluate(notInText), true);
});

test('subscript accepts integers only, and supports Python negative indexes', () => {
  const pair = { t: 'Tuple', elements: [constant('int', 10), constant('int', 20)] };
  assert.equal(numberOf(evaluate({ t: 'Subscript', value: pair, index: constant('int', 1) })), 20);
  assert.equal(numberOf(evaluate({ t: 'Subscript', value: pair, index: constant('int', -1) })), 20);
  assert.throws(
    () => evaluate({ t: 'Subscript', value: pair, index: constant('float', 1) }),
    (error) => error.message === 'Only integer indexes are allowed',
  );
  assert.throws(
    () => evaluate({ t: 'Subscript', value: pair, index: constant('int', 5) }),
    (error) => error.message === 'list index out of range',
  );
});

test('a call to a function outside the permitted set is refused', () => {
  const allowed = (value) => value;
  allowed.pyParams = ['value'];
  const forbidden = (value) => value;

  const call = (target) => ({
    t: 'Call',
    func: name(target),
    args: [constant('int', 1)],
    keywords: [],
  });

  assert.equal(
    numberOf(evaluate(call('allowed'), { allowed, forbidden }, { allowedCallables: [allowed] })),
    1,
  );
  assert.throws(
    () => evaluate(call('forbidden'), { allowed, forbidden }, { allowedCallables: [allowed] }),
    (error) => error.message === 'Function call is not allowed',
  );
});

test('keyword arguments bind by the callable parameter names', () => {
  const scale = (v_min, v_max = pyInt(10)) => pyInt(numberOf(v_min) + numberOf(v_max));
  scale.pyName = 'scale';
  scale.pyParams = ['v_min', 'v_max'];

  const call = {
    t: 'Call',
    func: name('scale'),
    args: [constant('int', 1)],
    keywords: [{ name: 'v_max', value: constant('int', 5) }],
  };
  assert.equal(numberOf(evaluate(call, { scale }, { allowedCallables: [scale] })), 6);

  const defaulted = { t: 'Call', func: name('scale'), args: [constant('int', 1)], keywords: [] };
  assert.equal(numberOf(evaluate(defaulted, { scale }, { allowedCallables: [scale] })), 11);

  const unknown = {
    t: 'Call',
    func: name('scale'),
    args: [constant('int', 1)],
    keywords: [{ name: 'nope', value: constant('int', 5) }],
  };
  assert.throws(
    () => evaluate(unknown, { scale }, { allowedCallables: [scale] }),
    (error) => error.message.includes("unexpected keyword argument 'nope'"),
  );
});

test('attribute access outside the permitted roots is refused', () => {
  const tree = { t: 'Attribute', value: name('DutyDB'), attr: 'OLTP' };
  assert.throws(
    () => evaluate(tree, { DutyDB }, { allowedAttributeRoots: [] }),
    (error) => error.message === 'Attribute access is not allowed',
  );
  assert.throws(
    () =>
      evaluate({ t: 'Attribute', value: name('DutyDB'), attr: 'NOPE' }, { DutyDB }, {
        allowedAttributeRoots: [DutyDB],
      }),
    (error) => error.message === 'Unknown rule attribute: NOPE',
  );
});

test('an unsupported node is rejected rather than silently ignored', () => {
  assert.throws(
    () => evaluate({ t: 'Lambda' }),
    (error) => error.message === 'Unsupported rule syntax: Lambda',
  );
});

test('a real exported expression evaluates against a hand-built context', () => {
  const expression = "'on' if pitr_enabled or replication_enabled else 'off'";
  const tree = rules.expressions[expression];
  assert.ok(tree, 'the expression is still part of the exported corpus');

  assert.equal(evaluate(tree, { pitr_enabled: false, replication_enabled: false }), 'off');
  assert.equal(evaluate(tree, { pitr_enabled: true, replication_enabled: false }), 'on');
  assert.equal(evaluate(tree, { pitr_enabled: false, replication_enabled: true }), 'on');
});

test('arithmetic inside an expression keeps Python promotion', () => {
  const tree = {
    t: 'BinOp',
    op: 'Div',
    left: constant('int', 8),
    right: constant('int', 2),
  };
  assert.equal(pyStr(evaluate(tree)), '4.0');

  const product = {
    t: 'BinOp',
    op: 'Mult',
    left: constant('int', 3),
    right: constant('int', 4),
  };
  assert.equal(pyStr(evaluate(product)), '12');
});

test('float division by zero raises, and is catchable as Python catches it', () => {
  const tree = { t: 'BinOp', op: 'Div', left: constant('int', 1), right: constant('int', 0) };
  assert.throws(() => evaluate(tree), (error) => error.name === 'ZeroDivisionError');
});

test('pyFloat is not silently produced by integer arithmetic', () => {
  const sum = { t: 'BinOp', op: 'Add', left: constant('int', 1), right: constant('int', 1) };
  assert.equal(pyStr(evaluate(sum)), '2');
  assert.equal(pyStr(pyFloat(2)), '2.0');
});
