/**
 * Evaluator for the exported rule expressions.
 *
 * The expressions are Python source. They are parsed in Python, by the same
 * `ast` module `RuleEvaluator` uses, and shipped as JSON trees by
 * `tools/export_web_data.py`; this module only walks those trees. Nothing here
 * parses Python, so the two implementations cannot disagree about what an
 * expression means — only about arithmetic, which is what the parity tests
 * measure.
 *
 * The node and operator set is exactly the one `RuleEvaluator` supports. The
 * exporter refuses to emit anything else, so an unsupported node reaching this
 * walker is a bug in the export, not user input.
 */

import {
  add,
  divide,
  isIntegral,
  isTruthy,
  multiply,
  numberOf,
  PyNumber,
  subtract,
} from './units.js';

export class RuleEvaluationError extends Error {
  constructor(message) {
    super(message);
    this.name = 'RuleEvaluationError';
  }
}

const BINARY_OPERATORS = {
  Add: add,
  Sub: subtract,
  Mult: multiply,
  Div: divide,
};

const COMPARISON_OPERATORS = {
  Eq: (left, right) => pyEquals(left, right),
  NotEq: (left, right) => !pyEquals(left, right),
  Lt: (left, right) => pyOrder(left, right) < 0,
  LtE: (left, right) => pyOrder(left, right) <= 0,
  Gt: (left, right) => pyOrder(left, right) > 0,
  GtE: (left, right) => pyOrder(left, right) >= 0,
  In: (left, right) => pyContains(right, left),
  NotIn: (left, right) => !pyContains(right, left),
};

/** Build a frozen enum namespace whose members are interned singletons. */
export function makeEnum(name, members) {
  const namespace = { __enumName__: name };
  // `members` is an ordered list of [name, value] pairs: Python reports enum
  // choices in declaration order, and that order reaches a user in an error
  // message.
  for (const [member, value] of members) {
    namespace[member] = Object.freeze({ __enum__: name, name: member, value });
  }
  return Object.freeze(namespace);
}

function isEnumMember(value) {
  return Boolean(value) && typeof value === 'object' && typeof value.__enum__ === 'string';
}

function pyEquals(left, right) {
  if (isEnumMember(left) || isEnumMember(right)) {
    if (!isEnumMember(left) || !isEnumMember(right)) return false;
    return left.__enum__ === right.__enum__ && left.name === right.name;
  }
  if (typeof left === 'string' || typeof right === 'string') {
    return left === right;
  }
  if (left === null || right === null) {
    return left === right;
  }
  if (Array.isArray(left) || Array.isArray(right)) {
    if (!Array.isArray(left) || !Array.isArray(right)) return false;
    return left.length === right.length && left.every((item, index) => pyEquals(item, right[index]));
  }
  return numberOf(left) === numberOf(right);
}

function pyOrder(left, right) {
  if (typeof left === 'string' && typeof right === 'string') {
    if (left === right) return 0;
    return left < right ? -1 : 1;
  }
  const a = numberOf(left);
  const b = numberOf(right);
  if (a === b) return 0;
  return a < b ? -1 : 1;
}

function pyContains(container, item) {
  if (typeof container === 'string') {
    if (typeof item !== 'string') {
      throw new RuleEvaluationError('Membership on a string requires a string');
    }
    return container.includes(item);
  }
  if (Array.isArray(container)) {
    return container.some((element) => pyEquals(element, item));
  }
  throw new RuleEvaluationError('Membership test on an unsupported container');
}

/**
 * Evaluate a deliberately small, side-effect-free expression subset.
 *
 * `context`, `allowedCallables` and `allowedAttributeRoots` mirror the Python
 * constructor arguments; the permitted sets are exported by the data layer so
 * the port can assert it offers exactly the same surface.
 */
export class RuleEvaluator {
  constructor(context, { allowedCallables, allowedAttributeRoots }) {
    this.context = context;
    this.allowedCallables = new Set(allowedCallables);
    this.allowedAttributeRoots = new Set(allowedAttributeRoots);
  }

  evaluate(tree) {
    return this.#node(tree);
  }

  #node(node) {
    switch (node.t) {
      case 'Const':
        return constantValue(node);
      case 'Name': {
        if (!(node.id in this.context)) {
          throw new RuleEvaluationError(`Unknown rule name: ${node.id}`);
        }
        return this.context[node.id];
      }
      case 'List':
      case 'Tuple':
        return node.elements.map((item) => this.#node(item));
      case 'BinOp': {
        const operation = BINARY_OPERATORS[node.op];
        if (operation === undefined) {
          throw new RuleEvaluationError('Unsupported binary operator');
        }
        return operation(this.#node(node.left), this.#node(node.right));
      }
      case 'BoolOp':
        return this.#booleanOperation(node);
      case 'Compare':
        return this.#comparison(node);
      case 'IfExp':
        return this.#node(isTruthy(this.#node(node.test)) ? node.body : node.orelse);
      case 'Attribute': {
        const root = this.#node(node.value);
        if (!this.allowedAttributeRoots.has(root) || node.attr.startsWith('_')) {
          throw new RuleEvaluationError('Attribute access is not allowed');
        }
        if (!(node.attr in root)) {
          throw new RuleEvaluationError(`Unknown rule attribute: ${node.attr}`);
        }
        return root[node.attr];
      }
      case 'Call': {
        const target = this.#node(node.func);
        if (!this.allowedCallables.has(target)) {
          throw new RuleEvaluationError('Function call is not allowed');
        }
        const args = node.args.map((argument) => this.#node(argument));
        for (const keyword of node.keywords) {
          // Keyword arguments are bound by position through the callable's
          // declared parameter names; a hole left in between stays `undefined`
          // so the JavaScript default applies exactly where Python's would.
          const position = (target.pyParams ?? []).indexOf(keyword.name);
          if (position < 0) {
            throw new RuleEvaluationError(
              `${target.pyName ?? 'callable'}() got an unexpected keyword argument '${keyword.name}'`,
            );
          }
          args[position] = this.#node(keyword.value);
        }
        return target(...args);
      }
      case 'Subscript': {
        const value = this.#node(node.value);
        const index = this.#node(node.index);
        if (!isIntegral(index)) {
          throw new RuleEvaluationError('Only integer indexes are allowed');
        }
        return subscript(value, numberOf(index));
      }
      default:
        throw new RuleEvaluationError(`Unsupported rule syntax: ${node.t}`);
    }
  }

  /** Python's `and`/`or` return an operand, not a boolean. */
  #booleanOperation(node) {
    if (node.op === 'And') {
      let result = true;
      for (const value of node.values) {
        result = this.#node(value);
        if (!isTruthy(result)) return result;
      }
      return result;
    }
    if (node.op === 'Or') {
      let result = false;
      for (const value of node.values) {
        result = this.#node(value);
        if (isTruthy(result)) return result;
      }
      return result;
    }
    throw new RuleEvaluationError('Unsupported boolean operator');
  }

  /** Chained comparison, evaluated left to right and short-circuiting. */
  #comparison(node) {
    let left = this.#node(node.left);
    for (let index = 0; index < node.ops.length; index += 1) {
      const operation = COMPARISON_OPERATORS[node.ops[index]];
      if (operation === undefined) {
        throw new RuleEvaluationError('Unsupported comparison operator');
      }
      const right = this.#node(node.comparators[index]);
      if (!operation(left, right)) return false;
      left = right;
    }
    return true;
  }
}

function constantValue(node) {
  switch (node.k) {
    case 'int':
      return new PyNumber(node.v, 'int');
    case 'float':
      return new PyNumber(node.v, 'float');
    case 'bool':
      return node.v;
    case 'str':
      return node.v;
    case 'none':
      return null;
    default:
      throw new RuleEvaluationError(`Unsupported constant kind: ${node.k}`);
  }
}

function subscript(value, index) {
  if (!Array.isArray(value)) {
    throw new RuleEvaluationError('Subscript on an unsupported value');
  }
  const position = index < 0 ? value.length + index : index;
  if (position < 0 || position >= value.length) {
    throw new RuleEvaluationError('list index out of range');
  }
  return value[position];
}
