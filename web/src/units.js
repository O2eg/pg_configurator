/**
 * Python-compatible numbers, text and size units.
 *
 * The reference implementation is Python, and two of its behaviours reach the
 * user directly:
 *
 *   - integers and floats are different types. `str(4.0)` is `"4.0"` while
 *     `str(4)` is `"4"`, and a generated `postgresql.conf` prints exactly that
 *     string. `--disk-score 0` produces `random_page_cost = 4.0`; a port that
 *     used a single numeric type would print `4`.
 *   - `round(x, n)` rounds half to even on the binary value, while both
 *     `Math.round` and `toFixed` round halves away from zero. `--db-cpu 0.0625`
 *     reaches that difference through `round(value, 3)`.
 *
 * So numbers crossing the rule evaluator carry their Python type, and the
 * helpers here reproduce the arithmetic and formatting that go with it. Values
 * that never reach a rule or a printed setting stay ordinary JavaScript
 * numbers.
 */

/**
 * Python exception identities.
 *
 * `make_conf` catches `(RuleEvaluationError, TypeError, ValueError,
 * ZeroDivisionError)` around every rule, so which one is raised is control
 * flow, not just a message.
 */
export class PyValueError extends Error {
  constructor(message) {
    super(message);
    this.name = 'ValueError';
  }
}

export class PyTypeError extends Error {
  constructor(message) {
    super(message);
    this.name = 'TypeError';
  }
}

export class PyZeroDivisionError extends Error {
  constructor(message) {
    super(message);
    this.name = 'ZeroDivisionError';
  }
}

export const INT = 'int';
export const FLOAT = 'float';

/** A Python number: a value plus the type Python would have given it. */
export class PyNumber {
  constructor(value, kind) {
    if (typeof value !== 'number' || !Number.isFinite(value)) {
      throw new TypeError(`PyNumber requires a finite number, got ${value}`);
    }
    if (kind !== INT && kind !== FLOAT) {
      throw new TypeError(`Unknown numeric kind: ${kind}`);
    }
    if (kind === INT && !Number.isInteger(value)) {
      throw new TypeError(`Integer value is not integral: ${value}`);
    }
    // Python integers have no negative zero, so `int(-0.5)` is `0` while
    // `Math.trunc(-0.5)` is `-0`. Floats keep theirs: `-0.0` is a real float.
    this.value = kind === INT && Object.is(value, -0) ? 0 : value;
    this.kind = kind;
    Object.freeze(this);
  }

  get isInt() {
    return this.kind === INT;
  }
}

/** Tag a value as a Python int, truncating toward zero the way `int()` does. */
export function pyInt(value) {
  return new PyNumber(Math.trunc(numberOf(value)), INT);
}

/** Tag a value as a Python float. */
export function pyFloat(value) {
  return new PyNumber(numberOf(value), FLOAT);
}

/** The plain numeric value of a number, boolean or PyNumber. */
export function numberOf(value) {
  if (value instanceof PyNumber) return value.value;
  if (typeof value === 'boolean') return value ? 1 : 0;
  if (typeof value === 'number') return value;
  throw new TypeError(`Not a number: ${describe(value)}`);
}

/** True when a value behaves as a Python integer (bool is a subclass of int). */
export function isIntegral(value) {
  if (value instanceof PyNumber) return value.isInt;
  if (typeof value === 'boolean') return true;
  return false;
}

function describe(value) {
  if (value === null || value === undefined) return 'None';
  if (typeof value === 'string') return JSON.stringify(value);
  return String(value);
}

// --------------------------------------------------------------------------
// arithmetic
// --------------------------------------------------------------------------

function combine(left, right, operation) {
  const result = operation(numberOf(left), numberOf(right));
  const kind = isIntegral(left) && isIntegral(right) ? INT : FLOAT;
  return new PyNumber(result, kind);
}

export function add(left, right) {
  if (typeof left === 'string' || typeof right === 'string') {
    if (typeof left !== 'string' || typeof right !== 'string') {
      throw new TypeError('Cannot concatenate a string with a non-string');
    }
    return left + right;
  }
  return combine(left, right, (a, b) => a + b);
}

export function subtract(left, right) {
  return combine(left, right, (a, b) => a - b);
}

export function multiply(left, right) {
  return combine(left, right, (a, b) => a * b);
}

/** Python's `/` is true division: the result is always a float. */
export function divide(left, right) {
  const divisor = numberOf(right);
  if (divisor === 0) {
    throw new PyZeroDivisionError('division by zero');
  }
  return pyFloat(numberOf(left) / divisor);
}

// --------------------------------------------------------------------------
// builtins reachable from a rule
// --------------------------------------------------------------------------

/** Python truthiness: 0, 0.0, "", [], (), None and False are false. */
export function isTruthy(value) {
  if (value === null || value === undefined) return false;
  if (typeof value === 'boolean') return value;
  if (value instanceof PyNumber) return value.value !== 0;
  if (typeof value === 'number') return value !== 0;
  if (typeof value === 'string') return value.length > 0;
  if (Array.isArray(value)) return value.length > 0;
  return true;
}

/** Python's `int()`: truncate toward zero, parse a string, keep bools as 0/1. */
export function toInt(value) {
  if (typeof value === 'string') {
    const text = value.trim();
    if (!/^[+-]?\d+$/.test(text)) {
      throw new PyValueError(`invalid literal for int() with base 10: '${value}'`);
    }
    return new PyNumber(Number(text), INT);
  }
  return pyInt(numberOf(value));
}

/** Python's `float()`. */
export function toFloat(value) {
  if (typeof value === 'string') {
    const parsed = Number(value.trim());
    if (Number.isNaN(parsed)) {
      throw new PyValueError(`could not convert string to float: '${value}'`);
    }
    return pyFloat(parsed);
  }
  return pyFloat(numberOf(value));
}

/**
 * Python's `round`.
 *
 * With one argument it returns an int; with two it keeps the argument's type.
 * Rounding is half to even on the exact binary value, which is why this works
 * on the decomposed double through BigInt rather than through `toFixed`: the
 * two disagree on every exact tie, and `round(0.0625, 3)` is one.
 */
export function pyRound(value, digits) {
  const kind = isIntegral(value) ? INT : FLOAT;
  const x = numberOf(value);
  if (digits === undefined || digits === null) {
    return new PyNumber(roundHalfEvenToInteger(x), INT);
  }
  const places = Math.trunc(numberOf(digits));
  if (kind === INT && places >= 0) {
    return new PyNumber(x, INT);
  }
  return new PyNumber(roundToPlaces(x, places), kind);
}

function roundHalfEvenToInteger(x) {
  const floor = Math.floor(x);
  const rest = x - floor;
  if (rest > 0.5) return floor + 1;
  if (rest < 0.5) return floor;
  return floor % 2 === 0 ? floor : floor + 1;
}

/** Exact `mantissa * 2 ** exponent` decomposition of a non-negative double. */
function decompose(x) {
  const buffer = new DataView(new ArrayBuffer(8));
  buffer.setFloat64(0, x);
  const high = buffer.getUint32(0);
  const low = buffer.getUint32(4);
  const rawExponent = (high >>> 20) & 0x7ff;
  const rawMantissa = (BigInt(high & 0xfffff) << 32n) | BigInt(low);
  if (rawExponent === 0) {
    return { mantissa: rawMantissa, exponent: -1074 };
  }
  return { mantissa: rawMantissa | (1n << 52n), exponent: rawExponent - 1075 };
}

function roundToPlaces(x, places) {
  if (x === 0) return x;
  const negative = x < 0;
  const { mantissa, exponent } = decompose(Math.abs(x));
  // |x| = mantissa * 2**exponent; we want round_half_even(|x| * 10**places).
  let numerator = mantissa;
  let denominator = 1n;
  if (exponent >= 0) {
    numerator *= 1n << BigInt(exponent);
  } else {
    denominator = 1n << BigInt(-exponent);
  }
  if (places >= 0) {
    numerator *= 10n ** BigInt(places);
  } else {
    denominator *= 10n ** BigInt(-places);
  }

  let quotient = numerator / denominator;
  const remainder = numerator % denominator;
  const twice = remainder * 2n;
  if (twice > denominator || (twice === denominator && quotient % 2n === 1n)) {
    quotient += 1n;
  }

  const magnitude = places >= 0 ? Number(quotient) / 10 ** places : Number(quotient) * 10 ** -places;
  return negative ? -magnitude : magnitude;
}

/**
 * Python's `max`: the first maximal operand, returned as-is.
 *
 * The identity matters, not only the magnitude: `max(0.0, 0)` is the float and
 * `max(0, 0.0)` is the int, and the difference propagates into whether a
 * printed setting reads `4` or `4.0`.
 */
export function pyMax(...values) {
  if (values.length === 0) {
    throw new PyValueError('max() arg is an empty sequence');
  }
  let best = values[0];
  for (const value of values.slice(1)) {
    if (numberOf(value) > numberOf(best)) best = value;
  }
  return best;
}

/** Python's `min`: the first minimal operand, returned as-is. */
export function pyMin(...values) {
  if (values.length === 0) {
    throw new PyValueError('min() arg is an empty sequence');
  }
  let best = values[0];
  for (const value of values.slice(1)) {
    if (numberOf(value) < numberOf(best)) best = value;
  }
  return best;
}

/** Python's `math.ceil` and `math.floor`, which return ints. */
export function pyCeil(value) {
  return new PyNumber(Math.ceil(numberOf(value)), INT);
}

export function pyFloor(value) {
  return new PyNumber(Math.floor(numberOf(value)), INT);
}

// --------------------------------------------------------------------------
// text
// --------------------------------------------------------------------------

/**
 * Python's `str()` for the values a rule can produce.
 *
 * Floats follow Python's repr: an integral float keeps its `.0`, the switch to
 * exponent notation happens at 1e16 and below 1e-4, and the exponent is signed
 * and at least two digits. JavaScript switches at 1e21 and 1e-7 and writes
 * `1e-7`, so `String()` cannot be used directly for a printed setting value.
 */
export function pyStr(value) {
  if (value === null || value === undefined) return 'None';
  if (typeof value === 'boolean') return value ? 'True' : 'False';
  if (typeof value === 'string') return value;
  if (Array.isArray(value)) return `[${value.map(pyRepr).join(', ')}]`;
  if (value instanceof PyNumber) {
    return value.isInt ? String(value.value) : pyFloatRepr(value.value);
  }
  if (typeof value === 'number') {
    return Number.isInteger(value) ? String(value) : pyFloatRepr(value);
  }
  if (value && typeof value === 'object' && value.__enum__) {
    // BasicEnum.__str__ returns the member value.
    return String(value.value);
  }
  throw new TypeError(`Cannot render ${describe(value)} as Python text`);
}

function pyRepr(value) {
  return typeof value === 'string' ? `'${value}'` : pyStr(value);
}

export function pyFloatRepr(x) {
  if (Number.isNaN(x)) return 'nan';
  if (x === Infinity) return 'inf';
  if (x === -Infinity) return '-inf';

  const negative = x < 0 || Object.is(x, -0);
  // toExponential() without an argument yields the shortest digit string that
  // round-trips, which is what Python's repr is built from as well.
  const [mantissa, rawExponent] = Math.abs(x).toExponential().split('e');
  const exponent = Number(rawExponent);
  const digits = mantissa.replace('.', '');
  const sign = negative ? '-' : '';

  if (exponent < -4 || exponent >= 16) {
    const head = digits.length > 1 ? `${digits[0]}.${digits.slice(1)}` : digits;
    const exponentSign = exponent < 0 ? '-' : '+';
    const magnitude = String(Math.abs(exponent)).padStart(2, '0');
    return `${sign}${head}e${exponentSign}${magnitude}`;
  }
  if (exponent >= 0) {
    const whole = digits.padEnd(exponent + 1, '0').slice(0, exponent + 1);
    const fraction = digits.slice(exponent + 1) || '0';
    return `${sign}${whole}.${fraction}`;
  }
  return `${sign}0.${'0'.repeat(-exponent - 1)}${digits}`;
}

// --------------------------------------------------------------------------
// UnitConverter
// --------------------------------------------------------------------------

export const SYS_STD = [
  [1024 ** 4, 'T'],
  [1024 ** 3, 'G'],
  [1024 ** 2, 'M'],
  [1024, 'K'],
  [1, 'B'],
];

export const SYS_IEC = [
  [1024 ** 4, 'Ti'],
  [1024 ** 3, 'Gi'],
  [1024 ** 2, 'Mi'],
  [1024, 'Ki'],
  [1, ''],
];

export const SYS_ISO = [
  [1000 ** 4, 'T'],
  [1000 ** 3, 'G'],
  [1000 ** 2, 'M'],
  [1000, 'K'],
  [1, 'B'],
];

export const SYS_PG = [
  [1024 ** 4, 'TB'],
  [1024 ** 3, 'GB'],
  [1024 ** 2, 'MB'],
  [1024, 'kB'],
  [1, ''],
];

/**
 * `UnitConverter.size_to`.
 *
 * The Python loop keeps the last pair when nothing matches, so an unmatched
 * unit falls through to the smallest one rather than raising.
 */
export function sizeTo(bytes, system = SYS_ISO, unit = null) {
  const amount = numberOf(bytes);
  let factor = system[system.length - 1][0];
  let postfix = system[system.length - 1][1];
  for (const [candidateFactor, candidatePostfix] of system) {
    factor = candidateFactor;
    postfix = candidatePostfix;
    if ((unit === null && amount / 10 >= candidateFactor) || unit === candidatePostfix) {
      break;
    }
  }
  return String(Math.trunc(amount / factor)) + postfix;
}

const SIZE_PATTERN = /^\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*([A-Za-z]*)\s*$/;

/** `UnitConverter.size_from`. */
export function sizeFrom(value, system = SYS_ISO) {
  if (typeof value === 'boolean') {
    throw new PyValueError('Boolean values are not valid sizes');
  }
  if (value instanceof PyNumber) {
    return pyInt(value.value);
  }
  if (typeof value === 'number') {
    return pyInt(value);
  }

  const match = SIZE_PATTERN.exec(String(value));
  if (match === null) {
    throw new PyValueError(`Invalid size value: ${value}`);
  }
  const amount = Number(match[1]);
  const unit = match[2];
  for (const [factor, suffix] of system) {
    if (suffix === unit || (suffix === '' && unit === 'B')) {
      return pyInt(amount * factor);
    }
  }
  throw new PyValueError(`Unknown size unit in value: ${value}`);
}

const CPU_PATTERN = /^\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*(m?)\s*$/i;

/** `UnitConverter.size_cpu_to_ncores`. */
export function cpuToCores(value) {
  if (typeof value === 'boolean') {
    throw new PyValueError('Boolean values are not valid CPU values');
  }
  const match = CPU_PATTERN.exec(String(value instanceof PyNumber ? value.value : value));
  if (match === null) {
    throw new PyValueError(`Invalid CPU value: ${value}`);
  }
  let amount = Number(match[1]);
  if (match[2].toLowerCase() === 'm') {
    amount /= 1000;
  }
  return pyRound(pyFloat(amount), 3);
}
