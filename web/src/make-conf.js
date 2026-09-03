/**
 * Port of `PGConfigurator.make_conf` for every supported PostgreSQL major.
 *
 * The structure follows the Python source section by section and keeps its
 * variable names, so the two can be read side by side. That is the only
 * practical way to review a port of this size, and the differential tests are
 * built on the assumption that a divergence is a local edit rather than a
 * redesign.
 *
 * Numbers are ordinary JavaScript numbers throughout the calculation. Python's
 * int/float distinction is applied at one boundary only — where values enter
 * the rule context, the closure environment and the recorded calculation —
 * because that is where it becomes observable: a rule with `to_unit: as_is`
 * prints `str(raw_value)`, and `str(4.0)` is not `str(4)`. The types are not
 * guessed: each one is fixed by how Python computes that value, and the parity
 * suite compares them against a real run.
 */

import {
  numberOf,
  pyFloat,
  pyInt,
  pyMax,
  pyMin,
  pyRound,
  pyStr,
  PyValueError,
  sizeFrom,
  sizeTo,
  SYS_IEC,
  SYS_PG,
} from './units.js';
import { RuleEvaluationError, RuleEvaluator } from './rule-eval.js';
import { createRuleCallables } from './configurator.js';

const PAGE_SIZE = 8192;
const MEBIBYTE = 1024 ** 2;
const GIBIBYTE = 1024 ** 3;
const MIN_WORK_MEM_IN_BYTES = 1024 ** 2;
const DEFAULT_TEMP_BUFFERS_IN_BYTES = 8 * MEBIBYTE;
const BACKEND_MEMORY_RESERVE_IN_BYTES = 10 * MEBIBYTE;
const STATISTICS_TIERS = [100, 500, 1000, 2500, 5000];

const DEPRECATED = 'deprecated';

// ---------------------------------------------------------------------------
// small helpers that mirror module-level Python functions
// ---------------------------------------------------------------------------

/** `common.get_major_version`. */
export function getMajorVersion(version) {
  const match = /\d+/.exec(String(version));
  if (match === null) {
    throw new PyValueError(`Invalid PostgreSQL version: ${version}`);
  }
  return Number(match[0]);
}

function isNumber(value) {
  return typeof value === 'number' && Number.isFinite(value);
}

function isInteger(value) {
  return typeof value === 'number' && Number.isInteger(value);
}

function coerceEnum(value, enumNamespace, argumentName) {
  if (value && typeof value === 'object' && value.__enum__) {
    if (value.__enum__ === enumNamespace.__enumName__) return value;
  }
  for (const key of Object.keys(enumNamespace)) {
    if (key === '__enumName__') continue;
    if (enumNamespace[key].value === value) return enumNamespace[key];
  }
  const members = Object.keys(enumNamespace)
    .filter((key) => key !== '__enumName__')
    .map((key) => enumNamespace[key].value);
  throw new PyValueError(`${argumentName} must be one of: ${members.join(', ')}`);
}

function validateFractionGroup(values, groupName) {
  let total = 0;
  for (const [name, value] of Object.entries(values)) {
    if (typeof value === 'boolean' || !isNumber(value)) {
      throw new PyValueError(`${name} must be a number`);
    }
    if (value <= 0 || value > 1) {
      throw new PyValueError(`${name} must be greater than 0 and not greater than 1`);
    }
    total += value;
  }
  // math.isclose(total, 1.0, rel_tol=0, abs_tol=1e-9)
  if (Math.abs(total - 1.0) > 1e-9) {
    throw new PyValueError(`${groupName} must sum to 1.0`);
  }
}

/** Python's `format(value, '.2%')`. */
function pyPercent(value) {
  const scaled = numberOf(pyRound(pyFloat(value * 100), 2));
  return `${scaled.toFixed(2)}%`;
}

/** Mirrors PGConfigurator.memory_budget_part_limits and its total. */
const MEMORY_BUDGET_PART_LIMITS = {
  shared_buffers_part: 0.8,
  client_mem_part: 0.4,
  maintenance_mem_part: 0.4,
};
const MEMORY_BUDGET_TOTAL_LIMIT = 0.85;

/** Mirrors configurator.ADVISORY_SEVERITIES and its ordering. */
const ADVISORY_SEVERITIES = ['warning', 'assumption', 'info'];

/** Mirrors configurator.advisory. */
function advisory(code, severity, message, setting = null, actual = null) {
  if (!ADVISORY_SEVERITIES.includes(severity)) {
    throw new PyValueError(`Unknown advisory severity: ${severity}`);
  }
  return {
    code,
    severity,
    setting,
    actual: actual === null || actual === undefined ? null : pyStr(actual),
    message,
  };
}

/** Mirrors configurator.sort_advisories: Python's sort is stable, and so is this. */
/** Mirrors configurator.ASSUMED_PEAK_WAL_RATE. */
export const ASSUMED_PEAK_WAL_RATE = '4Mi';

/** One safe single-quoted PostgreSQL configuration value. */
export function quotePostgresqlConfValue(value) {
  const text = pyStr(value);
  if (/[\0\r\n]/.test(text)) {
    throw new PyValueError(
      'PostgreSQL configuration string values must not contain NUL or line breaks',
    );
  }
  return `'${text.replace(/\\/g, '\\\\').replace(/'/g, "''")}'`;
}

/** Undo quotePostgresqlConfValue for Patroni's structured representation. */
export function unquotePostgresqlConfValue(value) {
  const text = String(value);
  if (text.length < 2 || !text.startsWith("'") || !text.endsWith("'")) return text;
  const body = text.slice(1, -1);
  let result = '';
  for (let index = 0; index < body.length; index += 1) {
    const pair = body.slice(index, index + 2);
    if (pair === "''") {
      result += "'";
      index += 1;
    } else if (pair === '\\\\') {
      result += '\\';
      index += 1;
    } else {
      result += body[index];
    }
  }
  return result;
}

// Mirrors configurator._SYNCHRONOUS_STANDBY_PATTERN: the grammar of
// synchronous_standby_names, so that both implementations count standbys the
// same way.
const SYNCHRONOUS_STANDBY_PATTERN = /^\s*(?:(ANY|FIRST)\s+)?(?:(\d+)\s*)?\(\s*([^()]*?)\s*\)\s*$/i;

/** Mirrors configurator.parse_synchronous_standby_names. */
export function parseSynchronousStandbyNames(text) {
  const match = SYNCHRONOUS_STANDBY_PATTERN.exec(text);
  let method;
  let numSync;
  let namesText;
  if (match === null) {
    method = null;
    numSync = 1;
    namesText = text;
  } else {
    method = match[1] ? match[1].toUpperCase() : null;
    numSync = match[2] ? Number(match[2]) : 1;
    namesText = match[3];
  }
  const names = namesText
    .split(',')
    .filter((name) => name.trim())
    .map((name) => name.trim().replace(/^"+|"+$/g, ''));
  return [method, numSync, names];
}

function sortAdvisories(advisories) {
  return advisories
    .map((item, index) => [item, index])
    .sort(
      ([a, ai], [b, bi]) =>
        ADVISORY_SEVERITIES.indexOf(a.severity) - ADVISORY_SEVERITIES.indexOf(b.severity) ||
        ai - bi,
    )
    .map(([item]) => item);
}

/** Mirrors PGConfigurator.postgresql_eol_dates and PGConfigurator.support_horizon. */
const POSTGRESQL_EOL_DATES = {
  '9.6': '2021-11-11',
  10: '2022-11-10',
  11: '2023-11-09',
  12: '2024-11-21',
  13: '2025-11-13',
  14: '2026-11-12',
  15: '2027-11-11',
  16: '2028-11-09',
  17: '2029-11-08',
  18: '2030-11-14',
};
const SUPPORT_HORIZON = '2026-09-03';

function validateMemoryBudgetParts(values) {
  // Python sums in insertion order starting from 0, and so does this loop, so
  // both sides see the same binary result for the same three decimals.
  let allocated = 0;
  for (const [name, value] of Object.entries(values)) {
    if (typeof value === 'boolean' || !isNumber(value)) {
      throw new PyValueError(`${name} must be a number`);
    }
    const limit = MEMORY_BUDGET_PART_LIMITS[name];
    if (value <= 0 || value > limit) {
      throw new PyValueError(`${name} must be greater than 0 and not greater than ${limit}`);
    }
    allocated += value;
  }
  // math.isclose(allocated, limit, rel_tol=0, abs_tol=1e-9) on the Python side.
  if (
    allocated > MEMORY_BUDGET_TOTAL_LIMIT &&
    Math.abs(allocated - MEMORY_BUDGET_TOTAL_LIMIT) > 1e-9
  ) {
    throw new PyValueError(
      `Main memory budgets must use at most ${Math.round(MEMORY_BUDGET_TOTAL_LIMIT * 100)}% ` +
        `of available RAM; got ${pyPercent(allocated)}`,
    );
  }
}

function validatePositiveRange(minValue, maxValue, minName, maxName) {
  for (const [name, value] of [
    [minName, minValue],
    [maxName, maxValue],
  ]) {
    if (typeof value === 'boolean' || !isInteger(value) || value <= 0) {
      throw new PyValueError(`${name} must be a positive integer`);
    }
  }
  if (minValue > maxValue) {
    throw new PyValueError(`${minName} must not be greater than ${maxName}`);
  }
}

// ---------------------------------------------------------------------------
// pg_settings snapshot
// ---------------------------------------------------------------------------

/** Rebuild one version's snapshot from the packed export. */
export function loadSettingMetadata(snapshot, version) {
  const columns = snapshot.columns;
  const rows = new Map();
  const toRow = (values) => {
    const row = {};
    columns.forEach((column, index) => {
      row[column] = values[index];
    });
    return row;
  };

  if (version === snapshot.base_version) {
    for (const [name, values] of Object.entries(snapshot.base)) rows.set(name, toRow(values));
    return rows;
  }

  const delta = snapshot.versions[version];
  if (delta === undefined) {
    throw new PyValueError(`Unsupported PostgreSQL version: ${version}`);
  }
  const absent = new Set(delta.absent);
  for (const [name, values] of Object.entries(snapshot.base)) {
    if (!absent.has(name)) rows.set(name, toRow(values));
  }
  for (const [name, values] of Object.entries(delta.changed)) rows.set(name, toRow(values));
  return rows;
}

function parsePgArray(value) {
  const text = (value ?? '').trim();
  if (!text || text === '{}') return new Set();
  const body = text.startsWith('{') && text.endsWith('}') ? text.slice(1, -1) : text;
  return new Set(
    body
      .split(',')
      .map((item) => item.trim().replace(/^"|"$/g, ''))
      .filter((item) => item.length > 0),
  );
}

const SIZE_FACTORS = {
  B: 1,
  kB: 1024,
  '8kB': 8 * 1024,
  MB: 1024 ** 2,
  '16MB': 16 * 1024 ** 2,
  GB: 1024 ** 3,
  TB: 1024 ** 4,
};

const TIME_FACTORS = { ms: 0.001, s: 1, min: 60, h: 3600, d: 86400 };

export function numericValueInSettingUnits(amount, sourceUnit, targetUnit) {
  if (!sourceUnit) return amount;
  if (sourceUnit in SIZE_FACTORS && targetUnit in SIZE_FACTORS) {
    return (amount * SIZE_FACTORS[sourceUnit]) / SIZE_FACTORS[targetUnit];
  }
  if (sourceUnit in TIME_FACTORS && targetUnit in TIME_FACTORS) {
    return (amount * TIME_FACTORS[sourceUnit]) / TIME_FACTORS[targetUnit];
  }
  throw new PyValueError(
    `Cannot convert PostgreSQL value from ${sourceUnit} to ${targetUnit || 'unitless'}`,
  );
}

const BOOLEAN_WORDS = new Set(['on', 'off', 'true', 'false', 'yes', 'no', '1', '0']);

function validateSettingValue(settingName, value, metadata, pgVersion) {
  const normalized = String(value).trim().replace(/^'|'$/g, '');
  const vartype = metadata.vartype ?? '';
  if (!vartype) return;

  if (vartype === 'bool') {
    if (!BOOLEAN_WORDS.has(normalized.toLowerCase())) {
      throw new PyValueError(`${settingName} must be a PostgreSQL boolean`);
    }
    return;
  }

  if (vartype === 'enum') {
    const enumValues = parsePgArray(metadata.enumvals);
    if (enumValues.size && !enumValues.has(normalized)) {
      throw new PyValueError(
        `${settingName}=${normalized} is not supported by PostgreSQL ${pgVersion}; ` +
          `expected one of: ${[...enumValues].sort().join(', ')}`,
      );
    }
    return;
  }

  if (vartype === 'integer' || vartype === 'real') {
    const match = /^([+-]?(?:\d+(?:\.\d*)?|\.\d+))([A-Za-z]+|8kB|16MB)?$/.exec(normalized);
    if (match === null) {
      throw new PyValueError(`${settingName}=${normalized} is not a valid PostgreSQL numeric value`);
    }
    const settingValue = numericValueInSettingUnits(
      Number(match[1]),
      match[2] ?? '',
      metadata.unit ?? '',
    );
    const minValue = metadata.min_val ?? '';
    const maxValue = metadata.max_val ?? '';
    let violated = null;
    if (minValue && settingValue < Number(minValue)) violated = ['min_val', minValue];
    else if (maxValue && settingValue > Number(maxValue)) violated = ['max_val', maxValue];
    if (violated !== null) {
      const [boundName, bound] = violated;
      throw new PyValueError(
        `${settingName}=${normalized} violates PostgreSQL ${pgVersion} ` +
          `${boundName}=${bound}${metadata.unit ?? ''}`,
      );
    }
  }
}

const APPLY_MODE_BY_CONTEXT = {
  postmaster: 'restart',
  sighup: 'reload',
  backend: 'reload_and_reconnect',
  'superuser-backend': 'reload_and_reconnect',
  superuser: 'reload',
  user: 'reload',
  internal: 'immutable',
  unknown: 'manual',
};

function applyModeForContext(context) {
  return APPLY_MODE_BY_CONTEXT[context] ?? 'manual';
}

function settingContext(settingName, settingsMetadata, restartRequiredSettings) {
  const context = settingsMetadata.get(settingName)?.context;
  if (context) return [context, 'pg_settings_snapshot'];
  if (settingName.includes('.')) return ['unknown', 'external_extension'];
  const fallback = restartRequiredSettings.has(settingName) ? 'postmaster' : 'sighup';
  return [fallback, 'compatibility_fallback'];
}

// ---------------------------------------------------------------------------
// rule-set preparation
// ---------------------------------------------------------------------------

const sortedVersions = (versioned) =>
  Object.keys(versioned).sort((left, right) => Number(left) - Number(right));

/**
 * Port of `PGConfigurator.prepare_alg_set`.
 *
 * Resolves `__parent` inheritance, deprecation and redefinition. The Python
 * original assigns `prepared_tune_alg[ver]` twice in a row; the first
 * assignment is discarded unread by the second, so only the second is ported.
 * Removing it changes nothing: deprecated names are filtered out of the
 * rebuilt list and again out of the inherited entries.
 */
export function prepareAlgSet(tuneAlg, sourceName) {
  if (tuneAlg === null || typeof tuneAlg !== 'object' || Array.isArray(tuneAlg)) {
    throw new PyValueError(`${sourceName} must be a versioned rule mapping`);
  }
  const prepared = {};
  for (const version of sortedVersions(tuneAlg)) {
    const versionAlgSet = tuneAlg[version];
    const deprecated = new Set(
      versionAlgSet.filter((rule) => rule.alg === DEPRECATED).map((rule) => rule.name),
    );

    prepared[version] = versionAlgSet
      .filter((rule) => rule.alg !== DEPRECATED && !('__parent' in rule))
      .map((rule) => ({ ...rule }));

    const currentNames = new Set(prepared[version].map((rule) => rule.name));
    const parentMarker = versionAlgSet.find((rule) => '__parent' in rule);
    const fromParent = parentMarker ? (prepared[parentMarker.__parent] ?? []) : [];

    for (const rule of fromParent) {
      if ('name' in rule && !currentNames.has(rule.name) && !deprecated.has(rule.name)) {
        prepared[version].push({ ...rule });
      }
    }
  }
  return prepared;
}

// ---------------------------------------------------------------------------
// the calculation
// ---------------------------------------------------------------------------

const DEFAULTS = {
  disk_type: 'SSD',
  duty_db: 'mixed',
  replication_enabled: null,
  replication_mode: null,
  pitr_enabled: true,
  synchronous_standby_names: '',
  replica_count: 1,
  logical_subscription_count: 0,
  pg_version: '18',
  reserved_ram_percent: 10,
  reserved_system_ram: '256Mi',
  shared_buffers_part: 0.25,
  client_mem_part: 0.2,
  maintenance_mem_part: 0.1,
  autovacuum_workers_mem_part: 0.5,
  maintenance_conns_mem_part: 0.5,
  min_conns: 20,
  max_conns: 500,
  min_autovac_workers: 3,
  max_autovac_workers: 20,
  min_maint_conns: 4,
  max_maint_conns: 16,
  platform: 'LINUX',
  common_conf: true,
  conf_profiles: null,
  disk_score: null,
  work_mem_concurrency_factor: 4.0,
  peak_wal_rate: null,
  replica_outage_tolerance: 900,
  wal_disk_budget: '32Gi',
  wal_segment_size: '16Mi',
  available_extensions: null,
  db_size: null,
};

/** Every default `make_conf` declares, for the CLI and the form to reuse. */
export function makeConfDefaults() {
  return { ...DEFAULTS };
}

export function makeConf(cpuCores, ramValue, options, data) {
  const settings = { ...DEFAULTS, ...options };
  const { rules, pgSettings, enums } = data;

  const {
    DiskType,
    DutyDB,
    Platform,
    ReplicationMode,
  } = enums;

  // --- input normalization and validation --------------------------------
  const pg_version = String(settings.pg_version);
  if (!rules.known_versions.includes(pg_version)) {
    throw new PyValueError(`Unsupported PostgreSQL version: ${pg_version}`);
  }

  const disk_type = coerceEnum(settings.disk_type, DiskType, 'disk_type');
  const duty_db = coerceEnum(settings.duty_db, DutyDB, 'duty_db');
  const platform = coerceEnum(settings.platform, Platform, 'platform');

  let replication_enabled = settings.replication_enabled;
  let replication_mode = settings.replication_mode;
  if (replication_enabled !== null && typeof replication_enabled !== 'boolean') {
    throw new PyValueError('replication_enabled must be a boolean or None');
  }
  if (replication_mode === null || replication_mode === undefined) {
    replication_mode =
      replication_enabled === false ? ReplicationMode.NONE : ReplicationMode.PHYSICAL;
  } else {
    replication_mode = coerceEnum(replication_mode, ReplicationMode, 'replication_mode');
  }
  if (
    replication_enabled !== null &&
    replication_enabled !== (replication_mode !== ReplicationMode.NONE)
  ) {
    throw new PyValueError(
      'replication_enabled conflicts with the explicitly selected replication_mode',
    );
  }
  replication_enabled = replication_mode !== ReplicationMode.NONE;

  const pitr_enabled = settings.pitr_enabled;
  const synchronous_standby_names = settings.synchronous_standby_names;
  const common_conf = settings.common_conf;
  if (typeof pitr_enabled !== 'boolean') {
    throw new PyValueError('pitr_enabled must be a boolean');
  }
  if (typeof synchronous_standby_names !== 'string') {
    throw new PyValueError('synchronous_standby_names must be a string');
  }
  if (/[\0\r\n]/.test(synchronous_standby_names)) {
    throw new PyValueError('synchronous_standby_names must not contain NUL or line breaks');
  }
  if (synchronous_standby_names.trim() && !replication_enabled) {
    throw new PyValueError('synchronous_standby_names requires physical or logical replication');
  }
  const [sync_method, sync_required, sync_candidates] =
    parseSynchronousStandbyNames(synchronous_standby_names);
  if (sync_method !== null && pg_version === '9.6') {
    throw new PyValueError(
      `synchronous_standby_names uses ${sync_method}, which PostgreSQL 10 introduced: ` +
        '9.6 accepts a list of names, optionally preceded by a count',
    );
  }
  if (typeof common_conf !== 'boolean') {
    throw new PyValueError('common_conf must be a boolean');
  }
  if (!common_conf) {
    throw new PyValueError(
      'common_conf cannot be disabled: CSV logging, auto_explain, and ' +
        'pg_stat_statements are part of the complete configuration contract',
    );
  }
  const conf_profiles = settings.conf_profiles;
  if (conf_profiles !== null && conf_profiles !== undefined && typeof conf_profiles !== 'string') {
    throw new PyValueError('conf_profiles must be a comma-separated string');
  }
  const available_extensions = settings.available_extensions;
  if (
    available_extensions !== null &&
    available_extensions !== undefined &&
    typeof available_extensions !== 'string' &&
    !Array.isArray(available_extensions)
  ) {
    throw new PyValueError(
      'available_extensions must be a comma-separated string or collection',
    );
  }

  let selected_profiles = [];
  if (conf_profiles) {
    selected_profiles = conf_profiles.split(',').map((item) => item.trim());
    if (selected_profiles.some((profile) => profile === '')) {
      throw new PyValueError('Profile name must not be empty');
    }
    if (new Set(selected_profiles).size !== selected_profiles.length) {
      throw new PyValueError('Profile names must not be repeated');
    }
    const unknown = selected_profiles.filter((profile) => !(profile in rules.profiles));
    if (unknown.length) {
      throw new PyValueError(`Unknown configuration profiles: ${unknown.join(', ')}`);
    }
    const unsupported = selected_profiles.filter(
      (profile) => !rules.profiles[profile].supported_versions.includes(pg_version),
    );
    if (unsupported.length) {
      throw new PyValueError(
        `Profiles do not support PostgreSQL ${pg_version}: ${unsupported.join(', ')}`,
      );
    }
    if (selected_profiles.includes('profile_1c') && selected_profiles.length !== 1) {
      throw new PyValueError(
        'profile_1c is an exclusive compatibility profile and cannot be combined ' +
          'with other configuration profiles',
      );
    }
  }

  const disk_score = settings.disk_score;
  if (disk_score !== null && disk_score !== undefined) {
    if (typeof disk_score === 'boolean' || !isNumber(disk_score)) {
      throw new PyValueError('disk_score must be a number');
    }
    if (disk_score < 0 || disk_score > 100) {
      throw new PyValueError('disk_score must be between 0 and 100');
    }
  }
  const work_mem_concurrency_factor = settings.work_mem_concurrency_factor;
  if (
    typeof work_mem_concurrency_factor === 'boolean' ||
    !isNumber(work_mem_concurrency_factor) ||
    work_mem_concurrency_factor < 1
  ) {
    throw new PyValueError('work_mem_concurrency_factor must be a number not less than 1');
  }

  const reserved_ram_percent = settings.reserved_ram_percent;
  if (typeof reserved_ram_percent === 'boolean' || !isNumber(reserved_ram_percent)) {
    throw new PyValueError('reserved_ram_percent must be a number');
  }
  if (reserved_ram_percent < 0 || reserved_ram_percent >= 100) {
    throw new PyValueError(
      'reserved_ram_percent must be greater than or equal to 0 and less than 100',
    );
  }

  const shared_buffers_part = settings.shared_buffers_part;
  const client_mem_part = settings.client_mem_part;
  const maintenance_mem_part = settings.maintenance_mem_part;
  validateMemoryBudgetParts({ shared_buffers_part, client_mem_part, maintenance_mem_part });

  const autovacuum_workers_mem_part = settings.autovacuum_workers_mem_part;
  const maintenance_conns_mem_part = settings.maintenance_conns_mem_part;
  validateFractionGroup(
    { autovacuum_workers_mem_part, maintenance_conns_mem_part },
    'Maintenance memory parts',
  );

  const { min_conns, max_conns, min_autovac_workers, max_autovac_workers } = settings;
  const { min_maint_conns, max_maint_conns } = settings;
  validatePositiveRange(min_conns, max_conns, 'min_conns', 'max_conns');
  validatePositiveRange(
    min_autovac_workers,
    max_autovac_workers,
    'min_autovac_workers',
    'max_autovac_workers',
  );
  if (selected_profiles.includes('profile_1c') && max_autovac_workers < 4) {
    throw new PyValueError('profile_1c requires max_autovac_workers to be at least 4');
  }
  validatePositiveRange(min_maint_conns, max_maint_conns, 'min_maint_conns', 'max_maint_conns');

  const replica_count = settings.replica_count;
  const logical_subscription_count = settings.logical_subscription_count;
  for (const [name, value] of [
    ['replica_count', replica_count],
    ['logical_subscription_count', logical_subscription_count],
  ]) {
    if (typeof value === 'boolean' || !isInteger(value) || value < 0) {
      throw new PyValueError(`${name} must be a non-negative integer`);
    }
  }
  if (pg_version === '9.6' && logical_subscription_count) {
    throw new PyValueError('logical_subscription_count requires PostgreSQL 10 or newer');
  }
  if (logical_subscription_count && replication_mode !== ReplicationMode.LOGICAL) {
    throw new PyValueError('logical_subscription_count requires replication_mode=logical');
  }
  const replica_outage_tolerance = settings.replica_outage_tolerance;
  if (
    typeof replica_outage_tolerance === 'boolean' ||
    !isInteger(replica_outage_tolerance) ||
    replica_outage_tolerance < 0
  ) {
    throw new PyValueError('replica_outage_tolerance must be a non-negative integer');
  }

  const page_size = PAGE_SIZE;
  const mebibyte = MEBIBYTE;
  const gibibyte = GIBIBYTE;
  const min_work_mem_in_bytes = MIN_WORK_MEM_IN_BYTES;
  const default_temp_buffers_in_bytes = DEFAULT_TEMP_BUFFERS_IN_BYTES;
  const backend_memory_reserve_in_bytes = BACKEND_MEMORY_RESERVE_IN_BYTES;

  const total_cpu_cores = numberOf(
    (() => {
      const parsed = /^\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*(m?)\s*$/i.exec(String(cpuCores));
      if (typeof cpuCores === 'boolean') {
        throw new PyValueError('Boolean values are not valid CPU values');
      }
      if (parsed === null) {
        throw new PyValueError(`Invalid CPU value: ${cpuCores}`);
      }
      let amount = Number(parsed[1]);
      if (parsed[2].toLowerCase() === 'm') amount /= 1000;
      return pyRound(pyFloat(amount), 3);
    })(),
  );
  if (total_cpu_cores <= 0) {
    throw new PyValueError('cpu_cores must be greater than 0');
  }
  const cpu_threads = Math.max(1, Math.floor(total_cpu_cores));

  const ram_in_bytes = numberOf(sizeFrom(ramValue, SYS_IEC));
  const reserved_system_ram_in_bytes = numberOf(sizeFrom(settings.reserved_system_ram, SYS_IEC));
  if (ram_in_bytes <= 0) {
    throw new PyValueError('ram_value must be greater than 0');
  }
  if (reserved_system_ram_in_bytes < 0) {
    throw new PyValueError('reserved_system_ram must not be negative');
  }

  const available_ram_ratio = (100 - reserved_ram_percent) / 100;
  const total_ram_in_bytes = ram_in_bytes * available_ram_ratio - reserved_system_ram_in_bytes;
  if (total_ram_in_bytes <= 0) {
    throw new PyValueError(
      'Available RAM must be greater than 0 after reserved memory is subtracted',
    );
  }

  const db_size = settings.db_size;
  if (db_size !== null && db_size !== undefined && (typeof db_size !== 'string' || !db_size.trim())) {
    throw new PyValueError('db_size must be a non-empty IEC size string or None');
  }
  const database_size_in_bytes =
    db_size !== null && db_size !== undefined ? numberOf(sizeFrom(db_size, SYS_IEC)) : null;
  if (database_size_in_bytes !== null && database_size_in_bytes <= 0) {
    throw new PyValueError('db_size must be greater than 0');
  }

  const peak_wal_rate_source =
    settings.peak_wal_rate !== null && settings.peak_wal_rate !== undefined
      ? 'explicit'
      : 'default';
  const peak_wal_rate =
    peak_wal_rate_source === 'explicit' ? settings.peak_wal_rate : ASSUMED_PEAK_WAL_RATE;
  const peak_wal_rate_in_bytes = numberOf(sizeFrom(peak_wal_rate, SYS_IEC));
  const wal_disk_budget_in_bytes = numberOf(sizeFrom(settings.wal_disk_budget, SYS_IEC));
  const wal_segment_size_in_bytes = numberOf(sizeFrom(settings.wal_segment_size, SYS_IEC));
  if (peak_wal_rate_in_bytes <= 0) {
    throw new PyValueError('peak_wal_rate must be greater than 0');
  }
  if (wal_disk_budget_in_bytes < gibibyte) {
    throw new PyValueError('wal_disk_budget must be at least 1Gi');
  }
  if (
    wal_segment_size_in_bytes < mebibyte ||
    wal_segment_size_in_bytes > gibibyte ||
    (wal_segment_size_in_bytes & (wal_segment_size_in_bytes - 1)) !== 0
  ) {
    throw new PyValueError('wal_segment_size must be a power-of-two size between 1Mi and 1Gi');
  }
  if (wal_disk_budget_in_bytes < wal_segment_size_in_bytes * 8) {
    throw new PyValueError('wal_disk_budget must hold at least eight WAL segments');
  }

  const memory_allocated_part = shared_buffers_part + client_mem_part + maintenance_mem_part;
  const operating_headroom_part = 1.0 - memory_allocated_part;
  const connection_overhead_budget = total_ram_in_bytes * operating_headroom_part * 0.67;
  const connection_capacity = Math.trunc(
    connection_overhead_budget / backend_memory_reserve_in_bytes,
  );
  if (connection_capacity < min_conns) {
    throw new PyValueError(
      `Backend memory reserve supports ${connection_capacity} connections, ` +
        `less than min_conns=${min_conns}: add RAM, lower min_conns, or shrink the ` +
        'memory parts so that more headroom is left for backends',
    );
  }

  const connections_per_cpu = duty_db === DutyDB.OLTP ? 5 : 4;
  const duty_work_mem_cap_bytes = byDuty(duty_db, DutyDB, {
    FINANCIAL: 16 * mebibyte,
    OLTP: 32 * mebibyte,
    MIXED: 64 * mebibyte,
    STATISTIC: 256 * mebibyte,
  });
  const autovacuum_cpu_divisor = duty_db === DutyDB.OLTP ? 6 : 8;
  const autovacuum_naptime = byDuty(duty_db, DutyDB, {
    FINANCIAL: '30s',
    OLTP: '20s',
    MIXED: '30s',
    STATISTIC: '30s',
  });
  const autovacuum_vacuum_scale_factor = byDuty(duty_db, DutyDB, {
    FINANCIAL: 0.02,
    OLTP: 0.015,
    MIXED: 0.02,
    STATISTIC: 0.02,
  });
  const autovacuum_analyze_scale_factor = byDuty(duty_db, DutyDB, {
    FINANCIAL: 0.01,
    OLTP: 0.0075,
    MIXED: 0.01,
    STATISTIC: 0.01,
  });

  const maint_max_conns = Math.trunc(
    Math.min(Math.max(min_maint_conns, Math.ceil(cpu_threads / 8)), max_maint_conns),
  );

  const default_disk_scores = { SATA: 15, SAS: 30, NETWORK: 45, SSD: 75, NVME: 90 };
  const disk_scores =
    disk_score !== null && disk_score !== undefined ? disk_score : default_disk_scores[disk_type.name];

  // Python's min/max return an operand, so a value capped by an integer bound
  // becomes an int. These quantities are printed or recorded, so the winning
  // operand's type has to survive: they are computed on tagged numbers.
  const cpu_scores = pyMin(pyFloat((cpu_threads / 96) * 100), pyInt(100));
  const ram_scores = pyMin(pyFloat((total_ram_in_bytes / (768 * gibibyte)) * 100), pyInt(100));
  const system_scores = (numberOf(cpu_scores) + numberOf(ram_scores) + disk_scores) / 3;

  // calc_connection_scale, inlined here exactly as Python calls it
  const max_connections_value = (() => {
    if (connection_capacity < min_conns) {
      throw new PyValueError(
        `Client memory budget supports ${connection_capacity} connections, ` +
          `less than the required minimum ${min_conns}`,
      );
    }
    const cpu_target = min_conns + Math.max(0, cpu_threads - 1) * connections_per_cpu;
    return Math.trunc(
      Math.min(Math.max(cpu_target, min_conns), connection_capacity, max_conns),
    );
  })();

  const shared_buffers_bytes = total_ram_in_bytes * shared_buffers_part;
  const maintenance_budget_bytes = total_ram_in_bytes * maintenance_mem_part;
  const maintenance_work_mem_bytes = pyMin(
    pyFloat((maintenance_budget_bytes * maintenance_conns_mem_part) / maint_max_conns),
    pyInt(2 * gibibyte),
  );

  const autovacuum_workers = selected_profiles.includes('profile_1c')
    ? Math.trunc(
        Math.min(
          Math.max(min_autovac_workers, 4, Math.ceil(cpu_threads / 4)),
          max_autovac_workers,
        ),
      )
    : Math.trunc(
        Math.min(
          Math.max(min_autovac_workers, Math.ceil(cpu_threads / autovacuum_cpu_divisor) + 2),
          max_autovac_workers,
        ),
      );
  const autovacuum_work_mem_bytes = pyMin(
    pyFloat((maintenance_budget_bytes * autovacuum_workers_mem_part) / autovacuum_workers),
    pyInt(1024 * mebibyte),
  );

  let parallel_worker_budget;
  if (cpu_threads < 2 || total_ram_in_bytes < 2 * gibibyte) {
    parallel_worker_budget = 0;
  } else {
    const cpu_parallel_worker_budget = Math.max(
      1,
      Math.trunc(
        cpu_threads *
          byDuty(duty_db, DutyDB, { FINANCIAL: 0.25, OLTP: 0.35, MIXED: 0.5, STATISTIC: 0.75 }),
      ),
    );
    const ram_parallel_worker_budget = Math.max(
      1,
      Math.trunc(total_ram_in_bytes / (2 * gibibyte)),
    );
    parallel_worker_budget = Math.min(32, cpu_parallel_worker_budget, ram_parallel_worker_budget);
  }
  const logical_worker_budget =
    replication_mode === ReplicationMode.LOGICAL
      ? Math.min(16, Math.max(2, logical_subscription_count * 2 + 2))
      : 0;
  const extension_worker_reserve = common_conf || conf_profiles ? 4 : 2;
  const worker_process_budget = Math.min(
    64,
    Math.max(8, parallel_worker_budget + logical_worker_budget + extension_worker_reserve + 2),
  );
  const parallel_workers_per_gather = Math.min(
    parallel_worker_budget,
    byDuty(duty_db, DutyDB, { FINANCIAL: 2, OLTP: 2, MIXED: 4, STATISTIC: 8 }),
  );
  const parallel_maintenance_workers = Math.min(
    parallel_worker_budget,
    byDuty(duty_db, DutyDB, { FINANCIAL: 2, OLTP: 2, MIXED: 4, STATISTIC: 8 }),
  );
  const parallel_setup_cost = byDuty(duty_db, DutyDB, {
    FINANCIAL: 2000,
    OLTP: 1500,
    MIXED: 1000,
    STATISTIC: 500,
  });
  const parallel_tuple_cost = byDuty(duty_db, DutyDB, {
    FINANCIAL: 0.15,
    OLTP: 0.12,
    MIXED: 0.1,
    STATISTIC: 0.05,
  });
  const min_parallel_table_scan_size = byDuty(duty_db, DutyDB, {
    FINANCIAL: '32MB',
    OLTP: '16MB',
    MIXED: '8MB',
    STATISTIC: '4MB',
  });
  const min_parallel_index_scan_size = byDuty(duty_db, DutyDB, {
    FINANCIAL: '2MB',
    OLTP: '1MB',
    MIXED: '512kB',
    STATISTIC: '256kB',
  });
  const sync_workers_per_subscription = logical_worker_budget
    ? Math.min(4, Math.max(1, Math.floor(logical_worker_budget / 2)))
    : 0;
  const parallel_apply_workers = Math.min(4, Math.max(0, logical_worker_budget - 2));

  const effective_replica_count = replication_enabled ? replica_count : 0;
  const effective_logical_subscriptions =
    replication_mode === ReplicationMode.LOGICAL ? logical_subscription_count : 0;
  const replication_slot_budget = replication_enabled
    ? effective_replica_count + effective_logical_subscriptions + 2
    : 0;
  const wal_sender_budget = replication_enabled
    ? replication_slot_budget + effective_replica_count + 2
    : 0;
  const wal_level =
    replication_mode === ReplicationMode.LOGICAL
      ? 'logical'
      : replication_enabled || pitr_enabled
        ? 'replica'
        : 'minimal';
  const synchronous_commit =
    duty_db === DutyDB.FINANCIAL && synchronous_standby_names.trim() ? 'remote_apply' : 'on';
  const max_standby_streaming_delay = byDuty(duty_db, DutyDB, {
    FINANCIAL: '30s',
    OLTP: '45s',
    MIXED: '60s',
    STATISTIC: '5min',
  });

  const checkpoint_timeout_seconds = byDuty(duty_db, DutyDB, {
    FINANCIAL: 300,
    OLTP: 600,
    MIXED: 900,
    STATISTIC: 1800,
  });
  const checkpoint_timeout = `${Math.floor(checkpoint_timeout_seconds / 60)}min`;
  const max_wal_size_bytes = pyMin(
    pyMax(
      pyInt(peak_wal_rate_in_bytes * checkpoint_timeout_seconds * 2),
      pyInt(gibibyte),
      pyInt(wal_segment_size_in_bytes * 4),
    ),
    pyFloat(wal_disk_budget_in_bytes * 0.5),
  );
  const min_wal_size_bytes = pyMin(
    pyMax(pyFloat(numberOf(max_wal_size_bytes) / 4), pyInt(wal_segment_size_in_bytes * 2)),
    pyInt(4 * gibibyte),
  );
  // See the Python comment: retention is decided in whole segments because
  // that is how PostgreSQL spends the disk, and the segment being written is
  // held on top of what was asked for.
  const desired_wal_keep_bytes = peak_wal_rate_in_bytes * replica_outage_tolerance;
  const wal_keep_budget_bytes = wal_disk_budget_in_bytes * 0.4;
  const wal_keep_segments = replication_enabled
    ? Math.max(
        1,
        Math.min(
          Math.ceil(
            Math.max(desired_wal_keep_bytes, 512 * mebibyte) / wal_segment_size_in_bytes,
          ),
          Math.trunc(wal_keep_budget_bytes / wal_segment_size_in_bytes) - 1,
        ),
      )
    : 0;
  const wal_keep_bytes = pyInt(wal_keep_segments * wal_segment_size_in_bytes);
  const max_slot_wal_keep_size_bytes = replication_slot_budget
    ? wal_disk_budget_in_bytes * 0.4
    : 0;

  let effective_io_concurrency =
    disk_scores < 25 ? 2 : disk_scores < 50 ? 16 : disk_scores < 75 ? 64 : disk_scores < 90 ? 128 : 256;
  let maintenance_io_concurrency = Math.max(
    2,
    Math.min(64, Math.floor(effective_io_concurrency / 2)),
  );
  // See Python: before 18 these are posix_fadvise hints, which Windows lacks.
  const io_prefetch_available =
    platform !== Platform.WINDOWS || getMajorVersion(pg_version) >= 18;
  if (!io_prefetch_available) {
    effective_io_concurrency = 0;
    maintenance_io_concurrency = 0;
  }
  const random_page_cost = numberOf(pyRound(pyFloat(4.0 - (disk_scores / 100) * 2.9), 2));

  let io_combine_limit_bytes;
  if (platform === Platform.WINDOWS) {
    io_combine_limit_bytes = 128 * 1024;
  } else {
    io_combine_limit_bytes = byDuty(duty_db, DutyDB, {
      FINANCIAL: 128 * 1024,
      OLTP: disk_scores >= 50 ? 256 * 1024 : 128 * 1024,
      MIXED: disk_scores >= 75 ? 512 * 1024 : 256 * 1024,
      STATISTIC: disk_scores >= 75 ? 1024 * 1024 : 512 * 1024,
    });
  }
  if (getMajorVersion(pg_version) === 17) {
    io_combine_limit_bytes = Math.min(io_combine_limit_bytes, 256 * 1024);
  }
  const io_max_combine_limit_bytes = io_combine_limit_bytes;

  let vacuum_buffer_usage_limit_bytes = pyMin(
    pyFloat(shared_buffers_bytes / 8),
    pyInt(
      byDuty(duty_db, DutyDB, {
        FINANCIAL: 1024 * 1024,
        OLTP: 2 * mebibyte,
        MIXED: 8 * mebibyte,
        STATISTIC: 32 * mebibyte,
      }),
    ),
  );
  if (selected_profiles.includes('profile_1c')) {
    vacuum_buffer_usage_limit_bytes = pyMin(vacuum_buffer_usage_limit_bytes, pyInt(2 * mebibyte));
  }

  const roundStatisticsTarget = (value) =>
    STATISTICS_TIERS.find((tier) => tier >= value) ?? STATISTICS_TIERS[STATISTICS_TIERS.length - 1];

  const statistics_cpu_cap =
    cpu_threads < 4 ? 500 : cpu_threads < 8 ? 1000 : cpu_threads < 16 ? 2500 : 5000;
  const statistics_ram_cap =
    total_ram_in_bytes < 8 * gibibyte
      ? 500
      : total_ram_in_bytes < 32 * gibibyte
        ? 1000
        : total_ram_in_bytes < 128 * gibibyte
          ? 2500
          : 5000;
  const statistics_resource_cap = Math.min(statistics_cpu_cap, statistics_ram_cap);
  const statistics_size_multiplier =
    database_size_in_bytes === null
      ? 1
      : database_size_in_bytes < 100 * gibibyte
        ? 1
        : database_size_in_bytes < 1024 * gibibyte
          ? 2
          : 4;
  const statistics_duty_base = byDuty(duty_db, DutyDB, {
    FINANCIAL: 500,
    OLTP: 500,
    MIXED: 1000,
    STATISTIC: 2500,
  });
  const statistics_duty_cap = byDuty(duty_db, DutyDB, {
    FINANCIAL: 1000,
    OLTP: 2500,
    MIXED: 5000,
    STATISTIC: 5000,
  });
  const default_statistics_target = Math.min(
    statistics_resource_cap,
    statistics_duty_cap,
    roundStatisticsTarget(statistics_duty_base * statistics_size_multiplier),
  );
  const profile_1c_statistics_target = Math.min(
    statistics_resource_cap,
    roundStatisticsTarget(1000 * statistics_size_multiplier),
  );
  const profile_backend_statistics_target = Math.min(
    statistics_resource_cap,
    Math.max(500, default_statistics_target),
  );

  const jit = duty_db === DutyDB.MIXED || duty_db === DutyDB.STATISTIC ? 'on' : 'off';
  const jit_above_cost = duty_db === DutyDB.STATISTIC ? 50000 : 100000;
  const jit_inline_above_cost = duty_db === DutyDB.STATISTIC ? 250000 : 500000;
  const jit_optimize_above_cost = duty_db === DutyDB.STATISTIC ? 250000 : 500000;
  const autovacuum_cost_limit = Math.trunc(500 + disk_scores * 20);
  const autovacuum_cost_delay_ms = disk_scores < 25 ? 10 : disk_scores < 60 ? 5 : 2;
  const available_ram_gib = total_ram_in_bytes / gibibyte;
  const lock_tier =
    available_ram_gib < 4
      ? 64
      : available_ram_gib < 16
        ? 128
        : available_ram_gib < 64
          ? 256
          : available_ram_gib < 256
            ? 512
            : 1024;

  let connection_lock_target = max_connections_value;
  if (selected_profiles.includes('profile_1c')) {
    // calc_connection_limit(1000, min_conns)
    if (1000 < min_conns) {
      throw new PyValueError(
        `desired_connections=1000 is less than the required minimum ${min_conns}`,
      );
    }
    if (connection_capacity < min_conns) {
      throw new PyValueError(
        `Backend memory reserve supports ${connection_capacity} connections, ` +
          `less than the required minimum ${min_conns}`,
      );
    }
    connection_lock_target = Math.trunc(Math.min(1000, connection_capacity, max_conns));
  }
  let max_locks_per_transaction = Math.min(
    2048,
    Math.max(lock_tier, 64 + Math.ceil(connection_lock_target / 4)),
  );
  if (selected_profiles.includes('profile_1c')) {
    max_locks_per_transaction = Math.min(2000, Math.max(512, max_locks_per_transaction));
  }
  const max_pred_locks_per_transaction = Math.min(
    1024,
    Math.max(64, Math.floor(max_locks_per_transaction / 2)),
  );
  const max_pred_locks_per_page = Math.min(
    16,
    Math.max(2, Math.floor(max_pred_locks_per_transaction / 64)),
  );
  const max_pred_locks_per_relation = Math.min(
    max_pred_locks_per_transaction,
    Math.max(64, Math.floor(max_pred_locks_per_transaction / 2)),
  );
  const hash_mem_multiplier =
    duty_db === DutyDB.FINANCIAL || duty_db === DutyDB.OLTP ? 1.5 : 2.0;
  const logical_connection_budget = Math.max(1, replication_slot_budget);
  const logical_decoding_work_mem_bytes = pyMin(
    pyInt(256 * mebibyte),
    pyMax(
      pyInt(64 * mebibyte),
      pyFloat((total_ram_in_bytes * client_mem_part * 0.25) / logical_connection_budget),
    ),
  );
  const estimated_lock_process_count =
    connection_lock_target + worker_process_budget + autovacuum_workers + wal_sender_budget;
  const estimated_lock_memory_bytes =
    max_locks_per_transaction * estimated_lock_process_count * 270 +
    max_pred_locks_per_transaction * connection_lock_target * 64;
  const estimated_logical_decoding_memory_bytes =
    replication_mode === ReplicationMode.LOGICAL && getMajorVersion(pg_version) >= 13
      ? numberOf(logical_decoding_work_mem_bytes) * Math.max(1, effective_logical_subscriptions)
      : 0;
  const effective_cache_size_bytes = Math.max(
    shared_buffers_bytes,
    total_ram_in_bytes -
      total_ram_in_bytes * client_mem_part -
      maintenance_budget_bytes -
      connection_lock_target * backend_memory_reserve_in_bytes -
      estimated_lock_memory_bytes -
      estimated_logical_decoding_memory_bytes,
  );
  const autovacuum_worker_slots = Math.min(
    max_autovac_workers,
    Math.max(autovacuum_workers, autovacuum_workers + 2),
  );
  const io_workers = Math.min(8, Math.max(3, Math.ceil(cpu_threads / 8)));
  const lock_timeout = byDuty(duty_db, DutyDB, {
    FINANCIAL: '5s',
    OLTP: '10s',
    MIXED: '15s',
    STATISTIC: '1min',
  });
  const statement_timeout = byDuty(duty_db, DutyDB, {
    FINANCIAL: '5min',
    OLTP: '15min',
    MIXED: '30min',
    STATISTIC: '4h',
  });
  const idle_in_transaction_session_timeout = byDuty(duty_db, DutyDB, {
    FINANCIAL: '5min',
    OLTP: '10min',
    MIXED: '15min',
    STATISTIC: '1h',
  });
  const idle_session_timeout = byDuty(duty_db, DutyDB, {
    FINANCIAL: '4h',
    OLTP: '6h',
    MIXED: '8h',
    STATISTIC: '24h',
  });
  const transaction_timeout = byDuty(duty_db, DutyDB, {
    FINANCIAL: '30min',
    OLTP: '1h',
    MIXED: '2h',
    STATISTIC: '8h',
  });
  const tcp_keepalives_idle_seconds = byDuty(duty_db, DutyDB, {
    FINANCIAL: 60,
    OLTP: 90,
    MIXED: 120,
    STATISTIC: 300,
  });
  const tcp_keepalives_interval_seconds = byDuty(duty_db, DutyDB, {
    FINANCIAL: 10,
    OLTP: 15,
    MIXED: 30,
    STATISTIC: 30,
  });
  const desired_tcp_keepalives_count = byDuty(duty_db, DutyDB, {
    FINANCIAL: 6,
    OLTP: 6,
    MIXED: 4,
    STATISTIC: 3,
  });
  const tcp_keepalives_count = platform === Platform.LINUX ? desired_tcp_keepalives_count : 0;
  const network_failure_detection_seconds = tcp_keepalives_count
    ? tcp_keepalives_idle_seconds + tcp_keepalives_interval_seconds * tcp_keepalives_count
    : null;
  const tcp_user_timeout =
    platform === Platform.LINUX ? `${network_failure_detection_seconds}s` : '0';
  const client_connection_check_interval =
    platform === Platform.LINUX
      ? byDuty(duty_db, DutyDB, { FINANCIAL: '5s', OLTP: '10s', MIXED: '10s', STATISTIC: '30s' })
      : '0';
  const replication_network_timeout = byDuty(duty_db, DutyDB, {
    FINANCIAL: '60s',
    OLTP: '90s',
    MIXED: '120s',
    STATISTIC: '300s',
  });
  const authentication_timeout = '30s';
  const deadlock_timeout = duty_db === DutyDB.STATISTIC ? '2s' : '1s';

  // --- extensions ---------------------------------------------------------
  const required_extensions = new Set(rules.extensions.mandatory_common);
  for (const profile of selected_profiles) {
    for (const name of rules.extensions.profile_dependencies[profile] ?? []) {
      required_extensions.add(name);
    }
  }
  const unsupported_extensions = [...required_extensions]
    .filter(
      (extension) =>
        !(rules.extensions.specs[extension]?.supported_versions ?? []).includes(pg_version),
    )
    .sort();
  if (unsupported_extensions.length) {
    throw new PyValueError(
      `Extensions have no bundled PostgreSQL ${pg_version} rules: ` +
        unsupported_extensions.join(', '),
    );
  }

  let normalized_available_extensions = null;
  if (available_extensions !== null && available_extensions !== undefined) {
    const items =
      typeof available_extensions === 'string'
        ? available_extensions.split(',')
        : available_extensions.map((item) => String(item));
    normalized_available_extensions = new Set(
      items.map((item) => item.trim()).filter((item) => item.length > 0),
    );
  }

  if (required_extensions.size && normalized_available_extensions !== null) {
    const missing_extensions = [...required_extensions]
      .filter((name) => !normalized_available_extensions.has(name))
      .sort();
    if (missing_extensions.length) {
      throw new PyValueError(
        `Required extensions are unavailable: ${missing_extensions.join(', ')}`,
      );
    }
  }

  const shared_preload_libraries_value = rules.extensions.preload_order
    .filter((extension) => required_extensions.has(extension))
    .join(',');
  const auto_explain_log_min_duration = byDuty(duty_db, DutyDB, {
    FINANCIAL: '5s',
    OLTP: '8s',
    MIXED: '10s',
    STATISTIC: '30s',
  });
  const auto_explain_sample_rate = byDuty(duty_db, DutyDB, {
    FINANCIAL: 0.01,
    OLTP: 0.015,
    MIXED: 0.02,
    STATISTIC: 0.05,
  });
  const log_transaction_sample_rate = byDuty(duty_db, DutyDB, {
    FINANCIAL: 0.0001,
    OLTP: 0.00025,
    MIXED: 0.0005,
    STATISTIC: 0.001,
  });
  const log_statement_sample_rate = byDuty(duty_db, DutyDB, {
    FINANCIAL: 0.001,
    OLTP: 0.0025,
    MIXED: 0.005,
    STATISTIC: 0.01,
  });
  const log_min_duration_statement = auto_explain_log_min_duration;

  // --- rule-set assembly --------------------------------------------------
  const effective_perf_alg_set = {};
  for (const [version, ruleList] of Object.entries(rules.rule_sets.perf)) {
    effective_perf_alg_set[version] = ruleList.map((rule) => ({ _source: 'base', ...rule }));
  }
  for (const profile of selected_profiles) {
    const profileAlgSet = rules.rule_sets.profiles[profile];
    for (const [version, profileRules] of Object.entries(profileAlgSet)) {
      if (!(version in effective_perf_alg_set)) {
        throw new PyValueError(
          `Profile ${profile} contains unsupported PostgreSQL version ${version}`,
        );
      }
      for (const rule of profileRules) {
        effective_perf_alg_set[version].push({ ...rule, _source: profile });
      }
    }
  }

  const perf_alg_set_res = {};
  for (const version of sortedVersions(effective_perf_alg_set)) {
    const merged = new Map();
    for (const rule of effective_perf_alg_set[version]) {
      if (!('name' in rule)) continue;
      const { name, ...rest } = rule;
      merged.set(name, rest);
    }
    const parent = effective_perf_alg_set[version].find((rule) => '__parent' in rule);
    perf_alg_set_res[version] = parent ? [{ __parent: parent.__parent }] : [{}];
    for (const [name, rest] of merged) {
      perf_alg_set_res[version].push({ name, ...rest });
    }
  }

  const prepared_alg_set = prepareAlgSet(perf_alg_set_res, 'conf_perf')[pg_version];

  if (common_conf) {
    const prepared_common = prepareAlgSet(rules.rule_sets.common, 'conf_common')[pg_version];
    for (const rule of prepared_common) rule._source = 'common';
    prepared_alg_set.push(...prepared_common);
    for (const profile of selected_profiles) {
      const profileCommon = rules.rule_sets.common_profiles[profile];
      if (profileCommon === undefined) continue;
      const preparedProfileCommon = prepareAlgSet(profileCommon, `conf_common:${profile}`)[
        pg_version
      ];
      for (const rule of preparedProfileCommon) rule._source = `common:${profile}`;
      prepared_alg_set.push(...preparedProfileCommon);
    }
  }

  const base_parameter_rules = new Map(
    prepareAlgSet(rules.rule_sets.perf, 'conf_perf_base')
      [pg_version].filter((rule) => 'name' in rule)
      .map((rule) => [rule.name, rule]),
  );

  // --- rule context -------------------------------------------------------
  const closureEnvironment = {
    cpu_threads: pyInt(cpu_threads),
    connection_capacity: pyInt(connection_capacity),
    connections_per_cpu: pyInt(connections_per_cpu),
    max_conns: pyInt(max_conns),
    total_ram_in_bytes: pyFloat(total_ram_in_bytes),
    client_mem_part: pyFloat(client_mem_part),
    default_temp_buffers_in_bytes: pyInt(default_temp_buffers_in_bytes),
    min_work_mem_in_bytes: pyInt(min_work_mem_in_bytes),
    work_mem_concurrency_factor: pyFloat(work_mem_concurrency_factor),
    hash_mem_multiplier: pyFloat(hash_mem_multiplier),
    mebibyte: pyInt(mebibyte),
    duty_work_mem_cap_bytes: pyInt(duty_work_mem_cap_bytes),
    disk_scores: numericBinding(disk_scores, disk_score !== null && disk_score !== undefined),
    system_scores: pyFloat(system_scores),
  };
  const runtime = createRuleCallables(closureEnvironment, enums);

  const rule_context = {
    ...runtime.context,
    autovacuum_cost_delay_ms: pyInt(autovacuum_cost_delay_ms),
    autovacuum_cost_limit: pyInt(autovacuum_cost_limit),
    autovacuum_naptime,
    autovacuum_analyze_scale_factor: pyFloat(autovacuum_analyze_scale_factor),
    autovacuum_vacuum_scale_factor: pyFloat(autovacuum_vacuum_scale_factor),
    autovacuum_worker_slots: pyInt(autovacuum_worker_slots),
    autovacuum_workers: pyInt(autovacuum_workers),
    autovacuum_work_mem_bytes,
    autovacuum_workers_mem_part: pyFloat(autovacuum_workers_mem_part),
    authentication_timeout,
    client_connection_check_interval,
    deadlock_timeout,
    default_statistics_target: pyInt(default_statistics_target),
    disk_scores: closureEnvironment.disk_scores,
    disk_type,
    duty_db,
    effective_cache_size_bytes: pyFloat(effective_cache_size_bytes),
    effective_io_concurrency: pyInt(effective_io_concurrency),
    hash_mem_multiplier: pyFloat(hash_mem_multiplier),
    idle_in_transaction_session_timeout,
    idle_session_timeout,
    io_combine_limit_bytes: pyInt(io_combine_limit_bytes),
    io_max_combine_limit_bytes: pyInt(io_max_combine_limit_bytes),
    io_workers: pyInt(io_workers),
    jit,
    jit_above_cost: pyInt(jit_above_cost),
    jit_inline_above_cost: pyInt(jit_inline_above_cost),
    jit_optimize_above_cost: pyInt(jit_optimize_above_cost),
    logical_decoding_work_mem_bytes,
    logical_worker_budget: pyInt(logical_worker_budget),
    lock_timeout,
    log_min_duration_statement,
    maint_max_conns: pyInt(maint_max_conns),
    maintenance_io_concurrency: pyInt(maintenance_io_concurrency),
    maintenance_conns_mem_part: pyFloat(maintenance_conns_mem_part),
    maintenance_mem_part: pyFloat(maintenance_mem_part),
    maintenance_work_mem_bytes,
    max_autovac_workers: pyInt(max_autovac_workers),
    max_connections: pyInt(max_connections_value),
    max_connections_value: pyInt(max_connections_value),
    max_conns: pyInt(max_conns),
    max_locks_per_transaction: pyInt(max_locks_per_transaction),
    max_pred_locks_per_page: pyInt(max_pred_locks_per_page),
    max_pred_locks_per_relation: pyInt(max_pred_locks_per_relation),
    max_pred_locks_per_transaction: pyInt(max_pred_locks_per_transaction),
    max_slot_wal_keep_size_bytes: numericBinding(
      max_slot_wal_keep_size_bytes,
      Boolean(replication_slot_budget),
    ),
    max_standby_streaming_delay,
    max_wal_size_bytes,
    min_autovac_workers: pyInt(min_autovac_workers),
    min_conns: pyInt(min_conns),
    min_parallel_index_scan_size,
    min_parallel_table_scan_size,
    min_wal_size_bytes,
    page_size: pyInt(page_size),
    parallel_apply_workers: pyInt(parallel_apply_workers),
    parallel_maintenance_workers: pyInt(parallel_maintenance_workers),
    parallel_setup_cost: pyInt(parallel_setup_cost),
    parallel_tuple_cost: pyFloat(parallel_tuple_cost),
    parallel_worker_budget: pyInt(parallel_worker_budget),
    parallel_workers_per_gather: pyInt(parallel_workers_per_gather),
    pitr_enabled,
    platform,
    random_page_cost: pyFloat(random_page_cost),
    replication_mode,
    replication_enabled,
    replication_network_timeout,
    replication_slot_budget: pyInt(replication_slot_budget),
    shared_buffers: pyFloat(shared_buffers_bytes),
    shared_buffers_bytes: pyFloat(shared_buffers_bytes),
    shared_buffers_part: pyFloat(shared_buffers_part),
    shared_preload_libraries_value,
    profile_1c_statistics_target: pyInt(profile_1c_statistics_target),
    profile_backend_statistics_target: pyInt(profile_backend_statistics_target),
    sync_workers_per_subscription: pyInt(sync_workers_per_subscription),
    statement_timeout,
    synchronous_commit,
    synchronous_standby_names,
    total_cpu_cores: pyFloat(total_cpu_cores),
    total_ram_in_bytes: pyFloat(total_ram_in_bytes),
    wal_keep_bytes,
    wal_keep_segments: pyInt(wal_keep_segments),
    wal_level,
    wal_sender_budget: pyInt(wal_sender_budget),
    worker_process_budget: pyInt(worker_process_budget),
    transaction_timeout,
    tcp_keepalives_count: pyInt(tcp_keepalives_count),
    tcp_keepalives_idle_seconds: pyInt(tcp_keepalives_idle_seconds),
    tcp_keepalives_interval_seconds: pyInt(tcp_keepalives_interval_seconds),
    tcp_user_timeout,
    checkpoint_timeout,
    connection_lock_target: pyInt(connection_lock_target),
    auto_explain_log_min_duration,
    auto_explain_sample_rate: pyFloat(auto_explain_sample_rate),
    log_statement_sample_rate: pyFloat(log_statement_sample_rate),
    log_transaction_sample_rate: pyFloat(log_transaction_sample_rate),
    vacuum_buffer_usage_limit_bytes,
  };

  const evaluator = new RuleEvaluator(rule_context, {
    allowedCallables: runtime.allowedCallables,
    allowedAttributeRoots: runtime.allowedAttributeRoots,
  });

  const calculateRuleValue = (param, source, ruleEvaluator) => {
    const param_name = param.name;
    const rule_expression = 'alg' in param ? param.alg.trim() : null;
    let raw_value;
    try {
      if ('const' in param) {
        raw_value = param.const;
      } else {
        const tree = rules.expressions[rule_expression];
        if (tree === undefined) {
          throw new RuleEvaluationError(`Unknown rule expression: ${rule_expression}`);
        }
        raw_value = ruleEvaluator.evaluate(tree);
      }
    } catch (error) {
      throw new RuleEvaluationError(
        `Failed to calculate ${param_name} from source ${source}: ${error.message}`,
      );
    }

    let formatted_value;
    if ('unit_postfix' in param) {
      formatted_value = pyStr(raw_value) + param.unit_postfix;
    } else if (param.to_unit === 'as_is') {
      formatted_value = pyStr(raw_value);
    } else if (param.to_unit === 'quote') {
      formatted_value = quotePostgresqlConfValue(raw_value);
    } else if ('alg' in param) {
      formatted_value = sizeTo(numberOf(raw_value), SYS_PG, param.to_unit ?? null);
    } else {
      formatted_value = pyStr(raw_value);
    }
    return { rule_expression, raw_value, formatted_value };
  };

  const base_parameter_values = new Map();
  if (selected_profiles.length) {
    const baseRuleContext = { ...rule_context };
    const baseRuleEvaluator = new RuleEvaluator(baseRuleContext, {
      allowedCallables: runtime.allowedCallables,
      allowedAttributeRoots: runtime.allowedAttributeRoots,
    });
    for (const [name, baseRule] of base_parameter_rules) {
      const { raw_value, formatted_value } = calculateRuleValue(
        baseRule,
        'base',
        baseRuleEvaluator,
      );
      base_parameter_values.set(name, formatted_value);
      if (isIdentifier(name)) baseRuleContext[name] = raw_value;
    }
  }

  // --- rule application ---------------------------------------------------
  const settings_metadata = loadSettingMetadata(pgSettings, pg_version);
  const restart_required = new Set(rules.restart_required_settings);
  let config_res = new Map();
  const parameter_details = new Map();
  const overrides = [];

  for (const param of prepared_alg_set) {
    if (!('name' in param)) continue;
    const param_name = param.name;
    const source = param._source ?? 'base';
    const { rule_expression, raw_value, formatted_value } = calculateRuleValue(
      param,
      source,
      evaluator,
    );

    if (parameter_details.has(param_name)) {
      overrides.push({
        parameter: param_name,
        from: parameter_details.get(param_name).source,
        value_from: parameter_details.get(param_name).value,
        to: source,
        value_to: formatted_value,
      });
    } else if (source !== 'base' && base_parameter_values.has(param_name)) {
      overrides.push({
        parameter: param_name,
        from: 'base',
        value_from: base_parameter_values.get(param_name),
        to: source,
        value_to: formatted_value,
      });
    }

    config_res.set(param_name, formatted_value);
    const [setting_context_value, context_source] = settingContext(
      param_name,
      settings_metadata,
      restart_required,
    );
    parameter_details.set(param_name, {
      value: formatted_value,
      raw_value,
      source,
      rule: rule_expression,
      rule_kind: rule_expression !== null ? 'expression' : 'constant',
      context: setting_context_value,
      context_source,
      apply_mode: applyModeForContext(setting_context_value),
    });

    if (isIdentifier(param_name)) {
      rule_context[param_name] = raw_value;
    }
  }

  config_res = new Map([...config_res.entries()].sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0)));

  const allowed_extension_prefixes = new Set([
    ...required_extensions,
    ...(normalized_available_extensions ?? []),
  ]);
  const snapshot_validated_prefixes = new Set(
    [...required_extensions].filter(
      (name) => rules.extensions.specs[name].settings_validation === 'pg_settings_snapshot',
    ),
  );
  validateConfigParameters(
    config_res,
    pg_version,
    settings_metadata,
    allowed_extension_prefixes,
    snapshot_validated_prefixes,
  );

  runSafetyInvariants({
    config_res,
    synchronous_standby_names,
    pitr_enabled,
    replication_enabled,
    selected_profiles,
    extension_worker_reserve,
    mandatoryExtensions: rules.extensions.mandatory_common,
  });

  // --- recorded state -----------------------------------------------------
  const actual_max_connections = Math.trunc(Number(config_res.get('max_connections')));
  const actual_active_sessions = Math.min(actual_max_connections, Math.max(4, cpu_threads * 2));
  const work_mem_bytes = numberOf(sizeFrom(config_res.get('work_mem'), SYS_PG));
  const temp_buffers_bytes = numberOf(sizeFrom(config_res.get('temp_buffers'), SYS_PG));
  const maintenance_work_mem_actual = numberOf(
    sizeFrom(config_res.get('maintenance_work_mem'), SYS_PG),
  );
  const autovacuum_work_mem_actual = numberOf(
    sizeFrom(config_res.get('autovacuum_work_mem'), SYS_PG),
  );
  const hash_multiplier_actual = Number(config_res.get('hash_mem_multiplier') ?? 1.0);
  const client_memory_envelope =
    actual_active_sessions *
    (temp_buffers_bytes + work_mem_bytes * work_mem_concurrency_factor * hash_multiplier_actual);
  const maintenance_memory_envelope =
    maintenance_work_mem_actual * maint_max_conns +
    autovacuum_work_mem_actual * Math.trunc(Number(config_res.get('autovacuum_max_workers')));
  let logical_decoding_memory_envelope = 0;
  if (replication_mode === ReplicationMode.LOGICAL && config_res.has('logical_decoding_work_mem')) {
    logical_decoding_memory_envelope =
      numberOf(sizeFrom(config_res.get('logical_decoding_work_mem'), SYS_PG)) *
      Math.max(1, effective_logical_subscriptions);
  }
  const max_worker_processes = Math.trunc(Number(config_res.get('max_worker_processes')));
  const lock_process_count =
    actual_max_connections +
    max_worker_processes +
    Math.trunc(Number(config_res.get('autovacuum_max_workers'))) +
    Math.trunc(Number(config_res.get('max_wal_senders')));
  const lock_memory_envelope =
    Math.trunc(Number(config_res.get('max_locks_per_transaction'))) * lock_process_count * 270 +
    Math.trunc(Number(config_res.get('max_pred_locks_per_transaction'))) *
      actual_max_connections *
      64;
  const memory_envelope_bytes =
    numberOf(sizeFrom(config_res.get('shared_buffers'), SYS_PG)) +
    client_memory_envelope +
    maintenance_memory_envelope +
    logical_decoding_memory_envelope +
    lock_memory_envelope +
    actual_max_connections * backend_memory_reserve_in_bytes;
  if (memory_envelope_bytes > total_ram_in_bytes * 0.9) {
    throw new PyValueError(
      'Calculated concurrent memory envelope exceeds 90% of available RAM: fewer CPU ' +
        'cores, a lower work_mem_concurrency_factor or client_mem_part, or more RAM ' +
        'brings it back inside',
    );
  }

  // --- advisories ---------------------------------------------------------
  // Mirrors the Python block of the same name, which reads config_res for the
  // same reason: anything built earlier describes a draft rather than the file
  // this run emits.
  const sizeText = (value) => sizeTo(Math.trunc(numberOf(value)), SYS_PG);
  const settingsText = (names) =>
    names.map((name) => `${name}=${config_res.get(name)}`).join(', ');

  const advisories = [];
  const major_version = getMajorVersion(pg_version);
  const retention_setting = config_res.has('wal_keep_size') ? 'wal_keep_size' : 'wal_keep_segments';

  if (shared_buffers_part >= 0.35) {
    advisories.push(
      advisory(
        'shared_buffers_crowds_os_cache',
        'warning',
        `shared_buffers_part=${pyStr(pyFloat(shared_buffers_part))} gives ` +
          `shared_buffers=${config_res.get('shared_buffers')}. Past roughly a third of ` +
          'available RAM the same pages tend to be held twice, once here and once in ' +
          'the kernel cache, and the second copy is the one that stops helping. The ' +
          "0.35 threshold is this tool's, not a PostgreSQL limit.",
        'shared_buffers',
        config_res.get('shared_buffers'),
      ),
    );
  }

  const shared_buffers_actual_bytes = numberOf(sizeFrom(config_res.get('shared_buffers'), SYS_PG));
  if (shared_buffers_actual_bytes >= 8 * gibibyte) {
    let huge_pages_text =
      `shared_buffers=${config_res.get('shared_buffers')} is mapped with the default ` +
      'huge_pages=try, which falls back to 4kB pages without a word when none are ' +
      'reserved; at this size every backend that walks the buffer pool builds page ' +
      'tables of its own and TLB misses become query latency. ';
    if (platform === Platform.WINDOWS) {
      huge_pages_text +=
        'On Windows large pages need the Lock pages in memory privilege for the ' +
        'service account, and huge_pages=on turns a missing grant into a startup ' +
        'error instead of a silent fallback.';
    } else {
      huge_pages_text += 'Reserve vm.nr_hugepages before the server starts';
      huge_pages_text +=
        major_version >= 15
          ? ': postgres -C shared_memory_size_in_huge_pages -D <datadir> prints the ' +
            'exact count'
          : ', sized from the shared memory the server logs at startup plus a margin';
      huge_pages_text +=
        major_version >= 17
          ? ', and huge_pages_status shows after start whether the mapping succeeded.'
          : '; huge_pages=on turns a failed reservation into a startup error ' +
            'instead of a silent fallback.';
    }
    advisories.push(
      advisory(
        'huge_pages_not_reserved',
        'assumption',
        huge_pages_text,
        'shared_buffers',
        config_res.get('shared_buffers'),
      ),
    );
  }

  if (disk_score === null || disk_score === undefined) {
    advisories.push(
      advisory(
        'disk_score_inferred',
        'assumption',
        `Storage score ${pyStr(disk_scores)} was inferred from disk_type=` +
          `${disk_type.value}; nothing was measured. It sets random_page_cost, ` +
          'effective_io_concurrency, the parallel-scan thresholds and the autovacuum ' +
          'cost limits, so a disk_type that describes the hardware badly moves all of ' +
          'them. Supply disk_score from measured IOPS and latency to replace the ' +
          'guess.',
        null,
        disk_scores,
      ),
    );
  }

  if (peak_wal_rate_source === 'default') {
    const wal_sizing_settings = [
      'max_wal_size',
      'min_wal_size',
      'wal_keep_size',
      'wal_keep_segments',
    ].filter(
      (name) => config_res.has(name) && (replication_enabled || !name.startsWith('wal_keep')),
    );
    advisories.push(
      advisory(
        'peak_wal_rate_assumed',
        'assumption',
        `peak_wal_rate was not supplied, so a peak of ${ASSUMED_PEAK_WAL_RATE} per ` +
          `second is assumed. It sizes ${settingsText(wal_sizing_settings)}, the ` +
          'settings most sensitive to it: measure the real peak (pg_current_wal_lsn ' +
          'deltas, pg_stat_wal from 14, or pg_diag) and pass it to replace the guess.',
        null,
        ASSUMED_PEAK_WAL_RATE,
      ),
    );
  }

  const hash_budget_text = config_res.has('hash_mem_multiplier')
    ? `hash_mem_multiplier=${config_res.get('hash_mem_multiplier')}`
    : `a factor of ${pyStr(pyFloat(hash_mem_multiplier))} for hash operations, which on ` +
      `PostgreSQL ${pg_version} have neither a hash_mem_multiplier to declare nor, in hash ` +
      'aggregation, a spill to disk when the estimate is wrong';
  advisories.push(
    advisory(
      'work_mem_budget_assumption',
      'assumption',
      `work_mem=${config_res.get('work_mem')} is the client memory budget divided by ` +
        `${actual_active_sessions} concurrent sessions, ` +
        `work_mem_concurrency_factor=${pyStr(pyFloat(work_mem_concurrency_factor))} ` +
        `allocations each, and ${hash_budget_text}. All three are assumptions about the ` +
        'workload rather than measurements; pg_diag reports what the real figures are.',
      'work_mem',
      config_res.get('work_mem'),
    ),
  );

  const hash_factor_text = config_res.has('hash_mem_multiplier')
    ? `hash_mem_multiplier=${config_res.get('hash_mem_multiplier')}`
    : `a hash factor of ${pyStr(pyFloat(hash_mem_multiplier))}`;
  const work_mem_exposure_bytes =
    actual_max_connections * work_mem_bytes * work_mem_concurrency_factor * hash_mem_multiplier;
  if (work_mem_exposure_bytes > ram_in_bytes) {
    advisories.push(
      advisory(
        'work_mem_worst_case_exceeds_ram',
        'warning',
        `work_mem=${config_res.get('work_mem')} was sized for ${actual_active_sessions} ` +
          `active sessions, but max_connections=${actual_max_connections} may all be ` +
          'busy at once: at ' +
          `work_mem_concurrency_factor=${pyStr(pyFloat(work_mem_concurrency_factor))} ` +
          `allocations each and ${hash_factor_text}, that is ` +
          `${sizeText(work_mem_exposure_bytes)} of query memory against ` +
          `${sizeText(ram_in_bytes)} of RAM. A pooler that keeps concurrency near ` +
          `${actual_active_sessions}, or a lower max_conns, is what keeps the worst case ` +
          'inside physical memory.',
        'work_mem',
        config_res.get('work_mem'),
      ),
    );
  }

  if (!selected_profiles.includes('profile_1c')) {
    const cpu_connection_target = min_conns + (cpu_threads - 1) * connections_per_cpu;
    if (
      actual_max_connections < cpu_connection_target &&
      (actual_max_connections === connection_capacity || actual_max_connections === max_conns)
    ) {
      const connection_bound =
        actual_max_connections === connection_capacity
          ? `the memory budget, which holds ${connection_capacity}`
          : `max_conns=${max_conns}`;
      advisories.push(
        advisory(
          'max_connections_capped',
          'info',
          `max_connections=${actual_max_connections}: min_conns=${min_conns} plus ` +
            `${cpu_threads - 1} further CPU cores at ${connections_per_cpu} ` +
            `connections each (${duty_db.value} duty) would give ` +
            `${cpu_connection_target}, and ${connection_bound} is what holds it ` +
            'down. A connection costs backend memory whether or not it is busy, so ' +
            'a pooler in front of the database is what makes more of them ' +
            'affordable; add RAM to move the memory bound, or raise max_conns when ' +
            'that is the ceiling.',
          'max_connections',
          actual_max_connections,
        ),
      );
    }
  }

  if (replication_enabled && desired_wal_keep_bytes > numberOf(wal_keep_bytes)) {
    advisories.push(
      advisory(
        'wal_retention_capped',
        'warning',
        `Keeping ${sizeText(desired_wal_keep_bytes)} of WAL for a ` +
          `${pyStr(replica_outage_tolerance)}s outage would need more than the 40% of ` +
          'wal_disk_budget this tool spends on retention, so ' +
          `${retention_setting}=${config_res.get(retention_setting)} is what is kept — ` +
          `${wal_keep_segments} segments, ` +
          `${sizeText(numberOf(wal_keep_bytes) + wal_segment_size_in_bytes)} on disk once ` +
          'the segment being written is counted. A replica absent for longer needs a ' +
          'fresh base backup: raise wal_disk_budget or lower ' +
          'replica_outage_tolerance.',
        retention_setting,
        config_res.get(retention_setting),
      ),
    );
  }

  const desired_max_wal_size_bytes = peak_wal_rate_in_bytes * checkpoint_timeout_seconds * 2;
  if (desired_max_wal_size_bytes > wal_disk_budget_in_bytes * 0.5) {
    const seconds_at_peak = Math.trunc(numberOf(max_wal_size_bytes) / peak_wal_rate_in_bytes);
    advisories.push(
      advisory(
        'max_wal_size_capped_by_wal_budget',
        'warning',
        'Two checkpoint intervals of WAL at ' +
          `peak_wal_rate=${sizeText(peak_wal_rate_in_bytes)}/s come to ` +
          `${sizeText(desired_max_wal_size_bytes)}, more than the half of ` +
          'wal_disk_budget this tool lets max_wal_size have, so ' +
          `max_wal_size=${config_res.get('max_wal_size')} is what fits: about ` +
          `${seconds_at_peak}s of peak writes against ` +
          `checkpoint_timeout=${config_res.get('checkpoint_timeout')}. Under sustained ` +
          'peak load checkpoints are then triggered by size, and each one restarts ' +
          'full-page writes, raising the WAL volume further. Raise wal_disk_budget, ' +
          'or lower peak_wal_rate if the peak was overstated.',
        'max_wal_size',
        config_res.get('max_wal_size'),
      ),
    );
  }

  if (replication_enabled && !config_res.has('max_slot_wal_keep_size')) {
    advisories.push(
      advisory(
        'replication_slot_retention_unbounded',
        'warning',
        `PostgreSQL ${pg_version} has no max_slot_wal_keep_size, so with ` +
          `max_replication_slots=${config_res.get('max_replication_slots')} a slot whose ` +
          'consumer has gone keeps every WAL segment since its restart_lsn until ' +
          `pg_wal fills the disk; ${retention_setting}=${config_res.get(retention_setting)} ` +
          'bounds only what is kept without a slot. Watch pg_replication_slots for ' +
          'inactive slots and drop the ones nobody will resume; PostgreSQL 13 adds ' +
          'the cap.',
        'max_replication_slots',
        config_res.get('max_replication_slots'),
      ),
    );
  }

  if (
    synchronous_standby_names.trim() &&
    sync_candidates.length > 0 &&
    !sync_candidates.includes('*') &&
    sync_required >= sync_candidates.length
  ) {
    const standby_text =
      sync_candidates.length === 1
        ? 'its only standby'
        : `every one of its ${sync_candidates.length} standbys`;
    const standby_example =
      major_version >= 10 ? 'FIRST 1 (s1, s2), or a quorum such as ANY 1 (s1, s2)' : '1 (s1, s2)';
    advisories.push(
      advisory(
        'synchronous_standbys_all_required',
        'warning',
        `synchronous_standby_names=${config_res.get('synchronous_standby_names')} needs ` +
          `${standby_text} for every commit: with one of them unreachable, each commit ` +
          `waits at synchronous_commit=${config_res.get('synchronous_commit')} until it is ` +
          'back or a superuser lowers synchronous_commit on the fly. Name more ' +
          'standbys than the count that must answer, for example ' +
          `${standby_example}.`,
        'synchronous_standby_names',
        config_res.get('synchronous_standby_names'),
      ),
    );
  }

  if (!replication_enabled && replica_count > 0) {
    advisories.push(
      advisory(
        'replica_count_ignored',
        'info',
        `replica_count=${replica_count} was given with replication_mode=none, so it ` +
          `changed nothing: max_wal_senders=${config_res.get('max_wal_senders')} and ` +
          `max_replication_slots=${config_res.get('max_replication_slots')} are what a ` +
          'cluster without replicas gets. Ask for replication_mode=physical if ' +
          'standbys are planned.',
        'max_wal_senders',
        config_res.get('max_wal_senders'),
      ),
    );
  } else if (
    replication_enabled &&
    effective_replica_count === 0 &&
    effective_logical_subscriptions === 0
  ) {
    advisories.push(
      advisory(
        'replication_without_replicas',
        'info',
        `replication_mode=${replication_mode.value} with replica_count=0: ` +
          `max_wal_senders=${config_res.get('max_wal_senders')} and ` +
          `max_replication_slots=${config_res.get('max_replication_slots')} keep room for ` +
          'base backups and one unplanned standby, and ' +
          `wal_level=${config_res.get('wal_level')} stays ready. Set replica_count once ` +
          'the standbys are known so that their senders and slots are provisioned.',
        'max_wal_senders',
        config_res.get('max_wal_senders'),
      ),
    );
  }

  if (config_res.get('wal_level') === 'logical') {
    let logical_text =
      'wal_level=logical logs the replica identity of every updated or deleted row, ' +
      'the whole old row where REPLICA IDENTITY FULL is set, so WAL volume follows ' +
      'the write mix and not only this setting. ' +
      `max_replication_slots=${config_res.get('max_replication_slots')} and ` +
      `max_wal_senders=${config_res.get('max_wal_senders')} are sized for ` +
      `logical_subscription_count=${effective_logical_subscriptions} and ` +
      `replica_count=${effective_replica_count}.`;
    if (config_res.has('logical_decoding_work_mem')) {
      logical_text +=
        ' logical_decoding_work_mem=' +
        `${config_res.get('logical_decoding_work_mem')} caps what a decoder holds ` +
        'before spilling to disk.';
    }
    if (major_version >= 17) {
      logical_text +=
        ' Slots do not survive a failover on their own: PostgreSQL 17 adds failover ' +
        'slots with synchronized_standby_slots here and sync_replication_slots on ' +
        'the standby, all left to the deployment by this file.';
    }
    if (config_res.has('idle_replication_slot_timeout')) {
      logical_text +=
        ' idle_replication_slot_timeout=' +
        `${config_res.get('idle_replication_slot_timeout')} invalidates a slot nobody ` +
        'consumes for that long, so a paused subscriber has to resume within it or ' +
        'be resynchronised.';
    }
    advisories.push(
      advisory('logical_replication_provisioned', 'info', logical_text, 'wal_level', config_res.get('wal_level')),
    );
  }

  if (duty_db === DutyDB.FINANCIAL && !synchronous_standby_names.trim()) {
    advisories.push(
      advisory(
        'financial_duty_without_synchronous_standby',
        'info',
        'Financial duty asks for the strongest durability on offer, but no ' +
          'synchronous_standby_names were supplied, so ' +
          `synchronous_commit=${config_res.get('synchronous_commit')} is as far as it ` +
          'goes: a commit is flushed to local disk and nothing remote is promised. ' +
          'Name a standby to get remote_apply.',
        'synchronous_commit',
        config_res.get('synchronous_commit'),
      ),
    );
  }

  if (config_res.get('wal_level') === 'minimal') {
    advisories.push(
      advisory(
        'wal_level_minimal',
        'info',
        'wal_level=minimal follows from asking for neither PITR nor replication. ' +
          'Crash recovery still works and committed transactions still survive a ' +
          'restart; what becomes impossible is point-in-time recovery, streaming a ' +
          'replica, and taking an online base backup. Losing the data directory then ' +
          'means restoring a cold copy.',
        'wal_level',
        config_res.get('wal_level'),
      ),
    );
  }

  const end_of_life_date = POSTGRESQL_EOL_DATES[pg_version] ?? null;
  const [horizon_year, horizon_remainder] = [
    SUPPORT_HORIZON.slice(0, SUPPORT_HORIZON.indexOf('-')),
    SUPPORT_HORIZON.slice(SUPPORT_HORIZON.indexOf('-') + 1),
  ];
  const horizon_next_year = `${Number(horizon_year) + 1}-${horizon_remainder}`;
  if (end_of_life_date !== null && end_of_life_date <= SUPPORT_HORIZON) {
    advisories.push(
      advisory(
        'postgresql_end_of_life',
        'warning',
        `PostgreSQL ${pg_version} reached end of life on ${end_of_life_date}: no ` +
          'further fixes are published for it, security ones included. This output ' +
          'is for legacy and test use.',
        null,
        end_of_life_date,
      ),
    );
  } else if (end_of_life_date !== null && end_of_life_date <= horizon_next_year) {
    advisories.push(
      advisory(
        'postgresql_end_of_life_approaching',
        'info',
        `PostgreSQL ${pg_version} reaches end of life on ${end_of_life_date}, ` +
          `within a year of this tool's support horizon of ${SUPPORT_HORIZON}. ` +
          'The major upgrade wants planning before then, not after.',
        null,
        end_of_life_date,
      ),
    );
  }

  if (pitr_enabled) {
    const incremental_backup_text =
      major_version >= 17
        ? ' PostgreSQL 17 can also take incremental base backups once summarize_wal=on, ' +
          'which this file leaves off.'
        : '';
    advisories.push(
      advisory(
        'pitr_transport_not_configured',
        'info',
        `pitr_enabled holds wal_level=${config_res.get('wal_level')}, which is the ` +
          'part of point-in-time recovery a configuration file can supply. Base ' +
          'backups, WAL archiving and the restore path are deployment work this tool ' +
          'does not do: pg_stand is one way to arrange it, pgBackRest and barman are ' +
          'others.' +
          incremental_backup_text,
        'wal_level',
        config_res.get('wal_level'),
      ),
    );
  }

  const network_settings = [
    'tcp_keepalives_idle',
    'tcp_keepalives_interval',
    'tcp_keepalives_count',
    'tcp_user_timeout',
    'client_connection_check_interval',
  ].filter((name) => config_res.has(name));
  advisories.push(
    advisory(
      'network_timeouts_are_baselines',
      'info',
      `PostgreSQL ${pg_version} on ${platform.value.toLowerCase()} takes ` +
        `${settingsText(network_settings)} from this file. They are baselines: a dead ` +
        'connection is noticed only as fast as the slowest layer in front of it allows, ' +
        'so align them with the load balancer, firewall, proxy and client-driver ' +
        'timeouts before applying.',
    ),
  );

  const workload_timeouts = [
    'statement_timeout',
    'lock_timeout',
    'idle_in_transaction_session_timeout',
    'idle_session_timeout',
    'transaction_timeout',
  ].filter((name) => config_res.has(name));
  advisories.push(
    advisory(
      'workload_timeouts_are_instance_wide',
      'info',
      `This file sets ${settingsText(workload_timeouts)} for the whole instance, ` +
        'maintenance and DBA sessions included, which suits a reproducible stand. In ' +
        'production the documented practice is ALTER ROLE and ALTER DATABASE, keeping ' +
        'one role less restricted so that a long recovery task can still run.',
    ),
  );

  if (config_res.get('password_encryption') === 'scram-sha-256') {
    advisories.push(
      advisory(
        'scram_password_encryption',
        'info',
        'password_encryption=scram-sha-256 applies to passwords stored from now on; ' +
          'existing md5 passwords keep working until they are set again. Check that ' +
          'every client driver in use speaks SCRAM before rotating them.',
        'password_encryption',
        config_res.get('password_encryption'),
      ),
    );
  }

  if (config_res.has('idle_session_timeout')) {
    advisories.push(
      advisory(
        'idle_session_timeout_and_poolers',
        'info',
        `idle_session_timeout=${config_res.get('idle_session_timeout')} closes idle ` +
          'sessions, including the ones a connection pooler is holding open on ' +
          'purpose. Verify that the pooler reconnects cleanly, or scope this timeout ' +
          'to interactive roles.',
        'idle_session_timeout',
        config_res.get('idle_session_timeout'),
      ),
    );
  }

  if (platform === Platform.WINDOWS) {
    // Only settings this version actually emits may be described; see Python.
    const system_default_settings = ['tcp_keepalives_count', 'tcp_user_timeout'].filter((name) =>
      config_res.has(name),
    );
    let windows_text =
      'Windows exposes neither TCP_KEEPCNT nor TCP_USER_TIMEOUT, so ' +
      `${settingsText(system_default_settings)} leaves detection of a dead peer to ` +
      'the operating-system defaults.';
    if (config_res.has('client_connection_check_interval')) {
      windows_text +=
        ' client_connection_check_interval=0 is not a system default but the check ' +
        'switched off: PostgreSQL implements it only where poll() offers POLLRDHUP, ' +
        'which Windows does not, so a backend keeps running a query for a client ' +
        'that has already gone.';
    }
    advisories.push(advisory('windows_network_options_unavailable', 'info', windows_text));
    if (!io_prefetch_available) {
      const prefetch_settings = ['effective_io_concurrency', 'maintenance_io_concurrency'].filter(
        (name) => config_res.has(name),
      );
      advisories.push(
        advisory(
          'windows_io_prefetch_unavailable',
          'info',
          `${settingsText(prefetch_settings)}: before PostgreSQL 18 these are ` +
            'posix_fadvise read-ahead hints, Windows has no posix_fadvise, and a ' +
            'Windows server refuses any other value at startup. Bitmap heap scans ' +
            'read one block at a time here; PostgreSQL 18 issues its own ' +
            'asynchronous I/O and takes the disk-derived values again.',
          'effective_io_concurrency',
          config_res.get('effective_io_concurrency'),
        ),
      );
    }
  }

  if (database_size_in_bytes === null) {
    advisories.push(
      advisory(
        'db_size_not_supplied',
        'assumption',
        'db_size was not supplied, so default_statistics_target=' +
          `${config_res.get('default_statistics_target')} comes from duty and hardware ` +
          'alone, with no database-size tier applied.',
        'default_statistics_target',
        config_res.get('default_statistics_target'),
      ),
    );
  }

  if (Math.trunc(Number(config_res.get('default_statistics_target'))) >= 2500) {
    advisories.push(
      advisory(
        'high_statistics_target',
        'info',
        `default_statistics_target=${config_res.get('default_statistics_target')} makes ` +
          'ANALYZE read more rows and the planner carry longer histograms, which costs ' +
          'planning time on every query rather than only on the skewed ones. Where the ' +
          'skew is in a few columns, ALTER TABLE ... ALTER COLUMN ... SET STATISTICS ' +
          'is the cheaper instrument.',
        'default_statistics_target',
        config_res.get('default_statistics_target'),
      ),
    );
  }

  if (selected_profiles.includes('profile_1c')) {
    advisories.push(
      advisory(
        'profile_1c_ssl_disabled',
        'warning',
        `profile_1c sets ssl=${config_res.get('ssl')}. Traffic between the 1C server ` +
          'and PostgreSQL is unencrypted, so the link between them has to be one you ' +
          'already trust.',
        'ssl',
        config_res.get('ssl'),
      ),
    );
    advisories.push(
      advisory(
        'profile_1c_row_security_disabled',
        'warning',
        `profile_1c sets row_security=${config_res.get('row_security')}. This does not ` +
          'read past row-level security: a query that would have a policy applied ' +
          'fails with an error instead, unless the role owns the table or holds ' +
          'BYPASSRLS. Any RLS in the database turns into an outage here, not a ' +
          'bypass.',
        'row_security',
        config_res.get('row_security'),
      ),
    );
    advisories.push(
      advisory(
        'profile_1c_standard_conforming_strings_disabled',
        'warning',
        'profile_1c sets standard_conforming_strings=' +
          `${config_res.get('standard_conforming_strings')}. Backslashes inside ordinary ` +
          'string literals become escape characters again, so existing SQL can parse ' +
          'into something else and an escaping mistake becomes exploitable. It is ' +
          'here because 1C emits literals written for that dialect.',
        'standard_conforming_strings',
        config_res.get('standard_conforming_strings'),
      ),
    );
    const requested_1c_connections = 1000;
    const binding_limits = [];
    if (actual_max_connections === requested_1c_connections) {
      binding_limits.push(`its own request of ${requested_1c_connections}`);
    }
    if (actual_max_connections === connection_capacity) {
      binding_limits.push(`the memory budget, which holds ${connection_capacity}`);
    }
    if (actual_max_connections === max_conns) {
      binding_limits.push(`max_conns=${max_conns}`);
    }
    advisories.push(
      advisory(
        'profile_1c_connection_target',
        'info',
        `profile_1c asks for ${requested_1c_connections} connections and ` +
          `max_connections=${actual_max_connections} is what came out, bound by ` +
          (binding_limits.join(' and ') || 'a rule from a later profile') +
          '. A connection costs memory whether or not it is running a query, so a ' +
          'pooler in front of the database is what keeps this number affordable.',
        'max_connections',
        actual_max_connections,
      ),
    );
    advisories.push(
      advisory(
        'profile_1c_file_limit_raised',
        'info',
        'profile_1c sets max_files_per_process=' +
          `${config_res.get('max_files_per_process')}, well above the default. The ` +
          'operating-system limit on open files has to be raised to match before this ' +
          'is applied, or backends start failing to open relations under load.',
        'max_files_per_process',
        config_res.get('max_files_per_process'),
      ),
    );
    const synchronous_commit_text =
      config_res.get('synchronous_commit') === 'remote_apply'
        ? 'synchronous_commit=remote_apply: financial duty with a named standby ' +
          'outranks the 1C performance guidance, so a commit waits for the standby to ' +
          'apply it, not merely to receive it. That is the slowest and safest of the ' +
          'settings on offer.'
        : `synchronous_commit=${config_res.get('synchronous_commit')}: every commit is ` +
          'flushed to local disk before it is acknowledged. 1C performance guidance ' +
          'permits turning this off and losing recent transactions on a crash; this ' +
          'tool does not.';
    advisories.push(
      advisory(
        'profile_1c_synchronous_commit',
        'info',
        `profile_1c leaves ${synchronous_commit_text}`,
        'synchronous_commit',
        config_res.get('synchronous_commit'),
      ),
    );
    advisories.push(
      advisory(
        'profile_1c_patched_gucs_omitted',
        'info',
        'profile_1c emits nothing that only a patched PostgreSQL understands, such ' +
          'as enable_temp_memory_catalog. A build that has those settings will not ' +
          'receive them from here without an explicit target-distribution contract.',
      ),
    );
  }

  if (required_extensions.size) {
    if (normalized_available_extensions === null) {
      advisories.push(
        advisory(
          'preload_modules_not_declared',
          'assumption',
          'Nothing was declared about what the target has installed, so ' +
            `shared_preload_libraries=${config_res.get('shared_preload_libraries')} is a ` +
            'requirement this run could not check. These are libraries loaded at ' +
            'startup rather than CREATE EXTENSION objects — auto_explain has no ' +
            'SQL-level extension at all — and a missing one keeps the server from ' +
            'starting. Pass --available-extensions to assert an inventory.',
          'shared_preload_libraries',
          config_res.get('shared_preload_libraries'),
        ),
      );
    } else {
      advisories.push(
        advisory(
          'extension_inventory_not_verified',
          'assumption',
          'The inventory is what the caller declared, not what the target ' +
            'answered: shared_preload_libraries=' +
            `${config_res.get('shared_preload_libraries')} was accepted on that word ` +
            'alone. Packaging, preloadability and the GUCs those modules add still ' +
            'have to be checked against the real server before this file is ' +
            'applied.',
          'shared_preload_libraries',
          config_res.get('shared_preload_libraries'),
        ),
      );
    }
  }

  if (config_res.get('wal_compression') === 'pglz') {
    advisories.push(
      advisory(
        'wal_compression_build_unknown',
        'assumption',
        'wal_compression=pglz is the one method every build carries. lz4 and zstd ' +
          'compress full-page images faster and smaller, but only a server built with ' +
          '--with-lz4 or --with-zstd accepts them, and nothing here knows how the ' +
          'target was built: where pg_config --configure lists the flag, set ' +
          'wal_compression=lz4, or zstd for the smaller output.',
        'wal_compression',
        config_res.get('wal_compression'),
      ),
    );
  }

  if (config_res.has('io_method')) {
    advisories.push(
      advisory(
        'io_method_worker_assumed',
        'assumption',
        `io_method=${config_res.get('io_method')} with io_workers=` +
          `${config_res.get('io_workers')}: PostgreSQL 18 runs asynchronous I/O through ` +
          'worker processes on every platform, while io_uring needs a build with ' +
          '--with-liburing and a kernel that permits it, neither of which this run ' +
          `can see. The ${config_res.get('io_workers')} workers serve every backend, and ` +
          `effective_io_concurrency=${config_res.get('effective_io_concurrency')} bounds ` +
          'the reads each backend keeps in flight; watch pg_stat_io before raising ' +
          'io_workers.',
        'io_method',
        config_res.get('io_method'),
      ),
    );
  }

  if (config_res.get('jit') === 'on') {
    advisories.push(
      advisory(
        'jit_requires_llvm_build',
        'assumption',
        'jit=on takes effect only in a server built with --with-llvm; elsewhere the ' +
          'setting is accepted and silently does nothing. pg_config --configure, or ' +
          'pg_jit_available() in a session, says which build this is.',
        'jit',
        config_res.get('jit'),
      ),
    );
  }

  advisories.push(
    advisory(
      'csv_log_retention_is_external',
      'info',
      `log_destination=${config_res.get('log_destination')} with rotation by size and by ` +
        'time. Rotation renames files; it never deletes them. A total disk limit for ' +
        'the log directory has to come from outside PostgreSQL.',
      'log_destination',
      config_res.get('log_destination'),
    ),
  );

  const sorted_advisories = sortAdvisories(advisories);

  const inputs = {
    available_extensions:
      normalized_available_extensions !== null
        ? [...normalized_available_extensions].sort()
        : null,
    common_conf,
    cpu_cores: total_cpu_cores,
    disk_score: disk_scores,
    disk_score_source: disk_score !== null && disk_score !== undefined ? 'explicit' : 'disk_type',
    disk_type: disk_type.value,
    db_size_bytes: database_size_in_bytes !== null ? Math.trunc(database_size_in_bytes) : null,
    duty_db: duty_db.value,
    logical_subscription_count: effective_logical_subscriptions,
    peak_wal_rate_bytes_per_second: peak_wal_rate_in_bytes,
    peak_wal_rate_source,
    pitr_enabled,
    pg_version,
    profiles: selected_profiles,
    ram_bytes: ram_in_bytes,
    replica_count: effective_replica_count,
    replica_outage_tolerance_seconds: replica_outage_tolerance,
    replication_enabled,
    replication_mode: replication_mode.value,
    reserved_ram_percent,
    reserved_system_ram_bytes: reserved_system_ram_in_bytes,
    synchronous_standby_names,
    wal_disk_budget_bytes: wal_disk_budget_in_bytes,
    wal_segment_size_bytes: wal_segment_size_in_bytes,
    work_mem_concurrency_factor,
  };

  const extensions = rules.extensions.preload_order
    .filter((extension) => required_extensions.has(extension))
    .map((extension) => ({
      availability:
        normalized_available_extensions !== null &&
        normalized_available_extensions.has(extension)
          ? 'declared_available'
          : 'unverified',
      availability_source: normalized_available_extensions !== null ? 'caller_inventory' : null,
      name: extension,
      provider: rules.extensions.specs[extension].provider,
      settings_validation: rules.extensions.specs[extension].settings_validation,
      // A copy: Python builds this with list(), and handing the caller the
      // bundled tuple itself lets one result's owner break every later run.
      supported_versions: [...rules.extensions.specs[extension].supported_versions],
    }));

  const calculation = {
    active_query_sessions: actual_active_sessions,
    available_ram_bytes: Math.trunc(total_ram_in_bytes),
    autovacuum_worker_budget: autovacuum_workers,
    autovacuum_naptime,
    autovacuum_analyze_scale_factor,
    autovacuum_vacuum_scale_factor,
    checkpoint_timeout_seconds,
    client_memory_envelope_bytes: Math.trunc(client_memory_envelope),
    connection_capacity,
    connections_per_cpu,
    cpu_score: numberOf(pyRound(cpu_scores, 4)),
    disk_score: numberOf(pyRound(closureEnvironment.disk_scores, 4)),
    effective_io_concurrency,
    effective_cache_size_bytes: Math.trunc(effective_cache_size_bytes),
    default_statistics_target,
    statistics_resource_cap,
    statistics_size_multiplier,
    profile_1c_statistics_target,
    profile_backend_statistics_target,
    parallel_setup_cost,
    parallel_tuple_cost,
    min_parallel_table_scan_size,
    min_parallel_index_scan_size,
    io_combine_limit_bytes: Math.trunc(io_combine_limit_bytes),
    io_max_combine_limit_bytes: Math.trunc(io_max_combine_limit_bytes),
    vacuum_buffer_usage_limit_bytes: Math.trunc(numberOf(vacuum_buffer_usage_limit_bytes)),
    logical_worker_budget,
    logical_decoding_memory_envelope_bytes: Math.trunc(logical_decoding_memory_envelope),
    lock_memory_envelope_bytes: Math.trunc(lock_memory_envelope),
    maintenance_memory_envelope_bytes: Math.trunc(maintenance_memory_envelope),
    memory_envelope_bytes: Math.trunc(memory_envelope_bytes),
    parallel_worker_budget,
    ram_score: numberOf(pyRound(ram_scores, 4)),
    network_failure_detection_seconds,
    replication_network_timeout_seconds: Math.trunc(
      Number(replication_network_timeout.replace(/s$/, '')),
    ),
    replication_slot_budget,
    wal_keep_bytes: Math.trunc(numberOf(wal_keep_bytes)),
    wal_sender_budget,
    worker_process_budget,
    work_mem_cap_bytes: duty_work_mem_cap_bytes,
  };

  return {
    config: Object.fromEntries(config_res),
    inputs,
    extensions,
    calculation,
    parameters: Object.fromEntries(
      [...parameter_details.entries()]
        .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
        .map(([key, detail]) => [
          key,
          {
            value: detail.value,
            raw_value: detail.raw_value,
            source: detail.source,
            rule: detail.rule,
            rule_kind: detail.rule_kind,
            context: detail.context,
            context_source: detail.context_source,
            apply_mode: detail.apply_mode,
          },
        ]),
    ),
    overrides,
    advisories: sorted_advisories,
  };
}

// ---------------------------------------------------------------------------
// helpers used by the calculation
// ---------------------------------------------------------------------------

function byDuty(duty, DutyDB, table) {
  for (const member of ['FINANCIAL', 'OLTP', 'MIXED', 'STATISTIC']) {
    if (duty === DutyDB[member]) return table[member];
  }
  throw new PyValueError(`Unhandled duty: ${duty?.value}`);
}

/**
 * Tag a value whose Python type depends on how it was produced.
 *
 * `disk_scores` is the caller's float when supplied and an int from the
 * `disk_type` table otherwise; `wal_keep_bytes` and `max_slot_wal_keep_size_bytes`
 * are `0` (int) when replication is off and a float otherwise. Getting this
 * wrong changes a printed value, so it is explicit rather than inferred.
 */
function numericBinding(value, isFloatSource) {
  return isFloatSource ? pyFloat(value) : pyInt(value);
}

function isIdentifier(name) {
  return /^[A-Za-z_][A-Za-z0-9_]*$/.test(name);
}

function validateConfigParameters(
  config,
  pgVersion,
  settingsMetadata,
  allowedExtensionPrefixes,
  snapshotValidatedPrefixes,
) {
  const unknown_settings = [...config.keys()]
    .filter((name) => !name.includes('.') && !settingsMetadata.has(name))
    .sort();
  if (unknown_settings.length) {
    throw new PyValueError(
      `Parameters are not supported by PostgreSQL ${pgVersion}: ${unknown_settings.join(', ')}`,
    );
  }

  const unknown_extension_settings = [...config.keys()]
    .filter((name) => name.includes('.') && !allowedExtensionPrefixes.has(name.split('.', 1)[0]))
    .sort();
  if (unknown_extension_settings.length) {
    throw new PyValueError(
      'Extension parameters were not declared by an enabled profile or ' +
        `available_extensions: ${unknown_extension_settings.join(', ')}`,
    );
  }

  const missing_snapshot = [...config.keys()]
    .filter(
      (name) =>
        name.includes('.') &&
        snapshotValidatedPrefixes.has(name.split('.', 1)[0]) &&
        !settingsMetadata.has(name),
    )
    .sort();
  if (missing_snapshot.length) {
    throw new PyValueError(
      `Extension parameters are not present in the PostgreSQL ${pgVersion} snapshot: ` +
        missing_snapshot.join(', '),
    );
  }

  for (const [name, value] of config) {
    const metadata = settingsMetadata.get(name);
    if (metadata !== undefined) {
      validateSettingValue(name, value, metadata, pgVersion);
    }
  }
}

const TIMEOUT_SECONDS = {
  '5s': 5,
  '10s': 10,
  '15s': 15,
  '1min': 60,
  '5min': 300,
  '10min': 600,
  '15min': 900,
  '30min': 1800,
  '1h': 3600,
  '2h': 7200,
  '4h': 14400,
  '8h': 28800,
};

function runSafetyInvariants({
  config_res,
  synchronous_standby_names,
  pitr_enabled,
  replication_enabled,
  selected_profiles,
  extension_worker_reserve,
  mandatoryExtensions,
}) {
  const get = (name, fallback) => (config_res.has(name) ? config_res.get(name) : fallback);

  if (get('full_page_writes') !== 'on' || get('fsync') !== 'on') {
    throw new PyValueError('Bundled safety invariant requires fsync=on and full_page_writes=on');
  }
  if (get('synchronous_commit') === 'remote_apply' && !synchronous_standby_names.trim()) {
    throw new PyValueError('synchronous_commit=remote_apply requires synchronous_standby_names');
  }
  if (!['on', 'remote_apply'].includes(get('synchronous_commit'))) {
    throw new PyValueError('Bundled safety invariant requires synchronous_commit=on or remote_apply');
  }
  if (get('wal_level') === 'minimal' && (pitr_enabled || replication_enabled)) {
    throw new PyValueError('wal_level=minimal conflicts with PITR or replication');
  }
  if (get('logging_collector') !== 'on' || !String(get('log_destination', '')).includes('csvlog')) {
    throw new PyValueError('Bundled observability invariant requires CSV logging collector');
  }
  const preloaded = new Set(
    String(get('shared_preload_libraries', ''))
      .replace(/^'|'$/g, '')
      .split(',')
      .map((item) => item.trim())
      .filter((item) => item.length > 0),
  );
  if (!mandatoryExtensions.every((name) => preloaded.has(name))) {
    throw new PyValueError(
      'Bundled observability invariant requires auto_explain and pg_stat_statements',
    );
  }

  const asInt = (name, fallback = 0) => Math.trunc(Number(get(name, fallback)));
  const max_worker_processes = asInt('max_worker_processes');
  const max_parallel_workers = Math.trunc(
    Number(get('max_parallel_workers', config_res.get('max_parallel_workers_per_gather'))),
  );
  const max_logical_workers = asInt('max_logical_replication_workers', 0);
  const required_worker_processes =
    max_parallel_workers + max_logical_workers + extension_worker_reserve + 2;
  if (max_worker_processes < required_worker_processes) {
    throw new PyValueError(
      'max_worker_processes must reserve capacity for parallel, logical, ' +
        'extension, and maintenance workers',
    );
  }
  if (asInt('max_sync_workers_per_subscription', 0) > max_logical_workers) {
    throw new PyValueError('max_sync_workers_per_subscription exceeds logical worker capacity');
  }
  if (asInt('max_parallel_apply_workers_per_subscription', 0) > max_logical_workers) {
    throw new PyValueError(
      'max_parallel_apply_workers_per_subscription exceeds logical worker capacity',
    );
  }
  if (asInt('max_parallel_workers_per_gather') > max_worker_processes) {
    throw new PyValueError('max_parallel_workers_per_gather exceeds worker-process capacity');
  }
  if (asInt('max_parallel_maintenance_workers', 0) > max_parallel_workers) {
    throw new PyValueError('max_parallel_maintenance_workers exceeds parallel worker capacity');
  }

  const reserved_connection_total =
    asInt('superuser_reserved_connections') + asInt('reserved_connections', 0);
  if (reserved_connection_total >= asInt('max_connections')) {
    throw new PyValueError(
      'superuser_reserved_connections plus reserved_connections must be below max_connections',
    );
  }
  if (
    numberOf(sizeFrom(get('effective_cache_size'), SYS_PG)) <
    numberOf(sizeFrom(get('shared_buffers'), SYS_PG))
  ) {
    throw new PyValueError('effective_cache_size must not be below shared_buffers');
  }
  if (
    numberOf(sizeFrom(get('min_wal_size'), SYS_PG)) >
    numberOf(sizeFrom(get('max_wal_size'), SYS_PG))
  ) {
    throw new PyValueError('min_wal_size must not exceed max_wal_size');
  }
  if (asInt('max_pred_locks_per_transaction') > asInt('max_locks_per_transaction')) {
    throw new PyValueError(
      'max_pred_locks_per_transaction must not exceed max_locks_per_transaction',
    );
  }
  for (const setting of ['max_pred_locks_per_page', 'max_pred_locks_per_relation']) {
    if (asInt(setting, 0) > asInt('max_pred_locks_per_transaction')) {
      throw new PyValueError(`${setting} must not exceed max_pred_locks_per_transaction`);
    }
  }
  if (
    asInt('autovacuum_worker_slots', 0) &&
    asInt('autovacuum_max_workers') > asInt('autovacuum_worker_slots')
  ) {
    throw new PyValueError('autovacuum_max_workers exceeds autovacuum_worker_slots');
  }
  if (!STATISTICS_TIERS.includes(asInt('default_statistics_target'))) {
    throw new PyValueError('default_statistics_target must use a bounded statistics tier');
  }
  if (config_res.has('jit')) {
    if (Number(get('jit_inline_above_cost')) < Number(get('jit_above_cost'))) {
      throw new PyValueError('jit_inline_above_cost must not be below jit_above_cost');
    }
    if (Number(get('jit_optimize_above_cost')) < Number(get('jit_above_cost'))) {
      throw new PyValueError('jit_optimize_above_cost must not be below jit_above_cost');
    }
  }
  if (
    config_res.has('io_max_combine_limit') &&
    numberOf(sizeFrom(get('io_combine_limit'), SYS_PG)) >
      numberOf(sizeFrom(get('io_max_combine_limit'), SYS_PG))
  ) {
    throw new PyValueError('io_combine_limit must not exceed io_max_combine_limit');
  }
  if (
    config_res.has('vacuum_buffer_usage_limit') &&
    numberOf(sizeFrom(get('vacuum_buffer_usage_limit'), SYS_PG)) >
      numberOf(sizeFrom(get('shared_buffers'), SYS_PG)) / 8
  ) {
    throw new PyValueError('vacuum_buffer_usage_limit must not exceed shared_buffers / 8');
  }
  if (selected_profiles.includes('profile_1c')) {
    if (asInt('max_locks_per_transaction') < 512) {
      throw new PyValueError('profile_1c requires at least 512 locks per transaction');
    }
    if (asInt('max_parallel_workers_per_gather') !== 0) {
      throw new PyValueError('profile_1c requires parallel query execution to be disabled');
    }
    if (get('enable_mergejoin') !== 'off') {
      throw new PyValueError('profile_1c requires enable_mergejoin=off');
    }
    if (config_res.has('jit') && get('jit') !== 'off') {
      throw new PyValueError('profile_1c requires jit=off');
    }
  }

  if (TIMEOUT_SECONDS[get('lock_timeout')] >= TIMEOUT_SECONDS[get('statement_timeout')]) {
    throw new PyValueError('lock_timeout must be shorter than statement_timeout');
  }
  if (
    config_res.has('transaction_timeout') &&
    TIMEOUT_SECONDS[get('transaction_timeout')] <=
      Math.max(
        TIMEOUT_SECONDS[get('statement_timeout')],
        TIMEOUT_SECONDS[get('idle_in_transaction_session_timeout')],
      )
  ) {
    throw new PyValueError(
      'transaction_timeout must exceed statement_timeout and ' +
        'idle_in_transaction_session_timeout',
    );
  }
}
