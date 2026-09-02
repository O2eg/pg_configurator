/**
 * The calculation core.
 *
 * At this stage it contains the surface a rule expression can reach: the eight
 * callables and six attribute roots `make_conf` hands to `RuleEvaluator`. The
 * budget calculation itself lands here next.
 *
 * Only five of the callables and one of the roots are referenced by any rule in
 * the current corpus, so the rest cannot be reached by the differential matrix
 * at all. They are ported anyway, and covered by their own unit tests, because
 * a future Python rule activates them without warning.
 */

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
  PyValueError,
  sizeFrom,
  sizeTo,
  subtract,
  SYS_IEC,
  SYS_ISO,
  SYS_PG,
  SYS_STD,
  toFloat,
  toInt,
} from './units.js';
import { makeEnum } from './rule-eval.js';

export { cpuToCores, sizeFrom, sizeTo };

/** Declare the parameter names a rule may use as keyword arguments. */
function declare(name, params, implementation) {
  implementation.pyName = name;
  implementation.pyParams = params;
  return implementation;
}

/**
 * Build the enum roots from the exported data.
 *
 * The members are interned, so `duty_db == DutyDB.FINANCIAL` compares two
 * references to the same object exactly as Python compares enum identities.
 */
export function createEnums(enumData) {
  const enums = {};
  for (const [name, members] of Object.entries(enumData)) {
    enums[name] = makeEnum(name, members);
  }
  return enums;
}

/**
 * The closures `make_conf` defines around its own locals.
 *
 * Python closes over the enclosing scope; the port takes the same values as an
 * explicit environment so the functions can be built — and tested — before the
 * rest of the calculation exists.
 */
export function createRuleCallables(environment, enums) {
  const {
    cpu_threads,
    connection_capacity,
    connections_per_cpu,
    max_conns,
    total_ram_in_bytes,
    client_mem_part,
    default_temp_buffers_in_bytes,
    min_work_mem_in_bytes,
    work_mem_concurrency_factor,
    hash_mem_multiplier,
    mebibyte,
    duty_work_mem_cap_bytes,
    disk_scores,
    system_scores,
  } = environment;

  const calc_cpu_scale = declare('calc_cpu_scale', ['v_min', 'v_max'], (v_min, v_max) => {
    if (numberOf(v_min) > numberOf(v_max)) {
      throw new PyValueError('v_min must not be greater than v_max');
    }
    const cpu_ratio = pyMin(pyMax(divide(subtract(cpu_threads, pyInt(1)), pyInt(95)), pyInt(0)), pyInt(1));
    return add(multiply(cpu_ratio, subtract(v_max, v_min)), v_min);
  });

  const calc_connection_scale = declare(
    'calc_connection_scale',
    ['v_min', 'v_max'],
    (v_min, v_max) => {
      if (numberOf(connection_capacity) < numberOf(v_min)) {
        throw new PyValueError(
          `Client memory budget supports ${pyIntText(connection_capacity)} connections, ` +
            `less than the required minimum ${pyIntText(v_min)}`,
        );
      }
      const cpu_target = add(
        v_min,
        multiply(pyMax(pyInt(0), subtract(cpu_threads, pyInt(1))), connections_per_cpu),
      );
      return toInt(pyMin(pyMax(cpu_target, v_min), connection_capacity, v_max));
    },
  );

  const calc_connection_limit = declare(
    'calc_connection_limit',
    ['desired_connections', 'required_minimum'],
    (desired_connections, required_minimum) => {
      if (numberOf(desired_connections) < numberOf(required_minimum)) {
        throw new PyValueError(
          `desired_connections=${pyIntText(desired_connections)} is less than the required ` +
            `minimum ${pyIntText(required_minimum)}`,
        );
      }
      if (numberOf(connection_capacity) < numberOf(required_minimum)) {
        throw new PyValueError(
          `Backend memory reserve supports ${pyIntText(connection_capacity)} connections, ` +
            `less than the required minimum ${pyIntText(required_minimum)}`,
        );
      }
      return toInt(pyMin(desired_connections, connection_capacity, max_conns));
    },
  );

  const calc_client_mem_values = declare(
    'calc_client_mem_values',
    ['connection_count', 'temp_buffers_part'],
    (connection_count, temp_buffers_part = pyFloat(0.1)) => {
      if (numberOf(connection_count) <= 0) {
        throw new PyValueError('connection_count must be greater than 0');
      }
      if (numberOf(temp_buffers_part) <= 0 || numberOf(temp_buffers_part) >= 1) {
        throw new PyValueError('temp_buffers_part must be greater than 0 and less than 1');
      }

      const active_query_sessions = pyMin(
        connection_count,
        pyMax(pyInt(4), multiply(cpu_threads, pyInt(2))),
      );
      const client_memory_budget = multiply(total_ram_in_bytes, client_mem_part);
      const temp_buffers_value = pyMin(
        pyMax(
          default_temp_buffers_in_bytes,
          divide(multiply(client_memory_budget, temp_buffers_part), active_query_sessions),
        ),
        multiply(pyInt(32), mebibyte),
      );
      const work_memory_budget = pyMax(
        subtract(client_memory_budget, multiply(temp_buffers_value, active_query_sessions)),
        multiply(
          multiply(multiply(min_work_mem_in_bytes, active_query_sessions), work_mem_concurrency_factor),
          hash_mem_multiplier,
        ),
      );
      let work_mem_value = divide(
        work_memory_budget,
        multiply(multiply(active_query_sessions, work_mem_concurrency_factor), hash_mem_multiplier),
      );
      work_mem_value = pyMin(pyMax(work_mem_value, min_work_mem_in_bytes), duty_work_mem_cap_bytes);
      return [work_mem_value, temp_buffers_value];
    },
  );

  const calc_disk_scale = declare('calc_disk_scale', ['v_min', 'v_max'], (v_min, v_max) =>
    add(multiply(divide(disk_scores, pyInt(100)), subtract(v_max, v_min)), v_min),
  );

  const calc_system_scores_scale = declare(
    'calc_system_scores_scale',
    ['v_min', 'v_max'],
    (v_min, v_max) =>
      add(multiply(divide(system_scores, pyInt(100)), subtract(v_max, v_min)), v_min),
  );

  const calc_synchronous_commit = declare(
    'calc_synchronous_commit',
    ['duty_db', 'synchronous_standby_names'],
    (duty_db, synchronous_standby_names = '') => {
      if (duty_db === enums.DutyDB.FINANCIAL && synchronous_standby_names.trim()) {
        return 'remote_apply';
      }
      return 'on';
    },
  );

  const size_from = declare('size_from', ['sys_bytes', 'system'], (sys_bytes, system = SYS_ISO) =>
    sizeFrom(sys_bytes, system),
  );

  const builtins = {
    int: declare('int', ['x'], toInt),
    float: declare('float', ['x'], toFloat),
    max: declare('max', [], pyMax),
    min: declare('min', [], pyMin),
    round: declare('round', ['number', 'ndigits'], pyRound),
  };

  const roots = {
    ...enums,
    PGConfigurator: Object.freeze({ calc_synchronous_commit }),
    UnitConverter: Object.freeze({
      size_from,
      size_to: declare('size_to', ['bytes', 'system', 'unit'], sizeTo),
      sys_std: SYS_STD,
      sys_iec: SYS_IEC,
      sys_iso: SYS_ISO,
      sys_pg: SYS_PG,
    }),
  };

  const context = {
    calc_client_mem_values,
    calc_connection_limit,
    calc_connection_scale,
    calc_cpu_scale,
    calc_disk_scale,
    calc_system_scores_scale,
    ...builtins,
    ...roots,
  };

  const allowedCallables = [
    calc_synchronous_commit,
    size_from,
    calc_client_mem_values,
    calc_connection_limit,
    calc_connection_scale,
    calc_cpu_scale,
    calc_disk_scale,
    calc_system_scores_scale,
    builtins.float,
    builtins.int,
    builtins.max,
    builtins.min,
    builtins.round,
  ];

  return {
    context,
    allowedCallables,
    allowedAttributeRoots: Object.values(roots),
    helpers: { pyCeil, pyFloor },
  };
}

function pyIntText(value) {
  return String(numberOf(value));
}
