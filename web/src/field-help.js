/**
 * What each input actually means, in more words than a CLI help line can spare.
 *
 * `--help` has to fit one terminal line per option, so it names the parameter
 * and stops. A form has room to say what the number changes and what happens
 * when it is left alone, which is the part a reader is usually missing.
 *
 * Every claim here is a statement about `make_conf`; when the calculation
 * changes, these change with it. `web/test/field-help.test.mjs` checks that the
 * table and the form agree on which fields exist.
 */

export const FIELD_HELP = {
  db_cpu:
    'Cores the cluster may use — not necessarily the cores the machine has. ' +
    'Decimals and Kubernetes millicores are accepted, so 500m is half a core.\n' +
    'Drives max_connections, the parallel-worker settings, autovacuum workers, ' +
    'and how many sessions are assumed to be running at the same time.',

  db_ram:
    'Physical RAM of the host, with an IEC suffix: Mi, Gi, Ti.\n' +
    'No budget is taken from this number directly. reserved-ram-percent and ' +
    'reserved-system-ram come off first, and everything else is a share of what ' +
    'is left.',

  db_size:
    'Logical size of the database, if it is known.\n' +
    'It selects a bounded default_statistics_target tier — a larger database ' +
    'earns larger planner samples. A dash in the box means no size was given: ' +
    'the target then comes from duty and hardware alone, and a warning records ' +
    'that the size tier was skipped.',

  db_disk_type:
    'Storage class behind the data directory.\n' +
    'It sets a default storage score — SATA 15, SAS 30, NETWORK 45, SSD 75, ' +
    'NVME 90 — which drives random_page_cost, effective_io_concurrency, the ' +
    'parallel-scan thresholds and the autovacuum cost limits. Set disk-score to ' +
    'override that default with a measured one.',

  disk_score:
    'A measured storage score from 0 to 100, replacing the default that ' +
    'disk-type implies.\n' +
    'Worth setting when the disk type describes the hardware badly: a throttled ' +
    'NVMe on a shared host, or SAS behind a large write-back cache. Left empty, ' +
    'the greyed number is the score disk-type is already giving you.',

  db_duty:
    'What the database mostly does.\n' +
    'oltp favours short transactions and frequent checkpoints; statistic favours ' +
    'large scans and long queries; financial trades throughput for durability; ' +
    'mixed sits between them. It moves the checkpoint interval, the statistics ' +
    'baseline, work_mem sizing, synchronous_commit and the I/O settings.',

  replication_mode:
    'The replication capability the cluster must have.\n' +
    'none keeps wal_level low. physical raises it for streaming replicas. ' +
    'logical adds the slots, senders and apply workers that logical decoding ' +
    'needs. This gates replica-count, logical-subscription-count and ' +
    'synchronous-standby-names.',

  pitr_enabled:
    'Keeps wal_level at a level from which point-in-time recovery is possible.\n' +
    'It preserves the capability and nothing more: taking base backups and ' +
    'shipping WAL somewhere is deployment work this tool does not do, and a ' +
    'warning says so on every run.',

  synchronous_standby_names:
    'The literal value for synchronous_standby_names — leave it empty for ' +
    'asynchronous replication.\n' +
    'Naming a standby is what makes durability truthful: with financial duty and ' +
    'a standby named, synchronous_commit becomes remote_apply. Without a name it ' +
    'stays on, because remote_apply would promise remote durability that nothing ' +
    'provides. Requires physical or logical replication.',

  replica_count:
    'How many physical replicas will stream from this node.\n' +
    'Sizes max_wal_senders and max_replication_slots together with the logical ' +
    'subscriptions, plus two spare slots for maintenance. Counted only when ' +
    'replication is enabled.',

  logical_subscription_count:
    'How many logical subscriptions this node will feed.\n' +
    'Adds replication slots and apply workers, and puts its own ' +
    'logical_decoding_work_mem into the memory envelope. Requires ' +
    'replication-mode = logical.',

  pg_version:
    'The PostgreSQL major version the configuration is for.\n' +
    'It selects which settings exist at all, their allowed ranges, and ' +
    'version-specific behaviour: wal_keep_segments before 13 and wal_keep_size ' +
    'from 13 on, io_method and io_max_concurrency on 18, and so on. A setting ' +
    'the version does not know is never emitted.',

  reserved_ram_percent:
    'Share of physical RAM withheld before any budget is computed — for the ' +
    'operating system, its page cache, and anything else living on the host.\n' +
    'Taken off first; reserved-system-ram is then subtracted from the remainder.',

  reserved_system_ram:
    'A flat amount withheld on top of reserved-ram-percent, for the things that ' +
    'do not grow with the size of the machine.\n' +
    'Subtracted after the percentage, so the two compose rather than compete.',

  shared_buffers_part:
    "Share of available RAM given to shared_buffers, PostgreSQL's own page " +
    'cache.\n' +
    'It may reach 0.8 — higher than the other two budgets — because it is one ' +
    'allocation made once at startup, not a per-session cost. Past a point it ' +
    'stops helping: the same pages are then held twice, once here and once in ' +
    'the kernel cache. The three main budgets together may not exceed 0.85.',

  client_mem_part:
    'Share of available RAM set aside for the memory queries use while they ' +
    'run: work_mem and temp_buffers.\n' +
    'Every point here is multiplied by the sessions assumed to be active at ' +
    'once, so it buys far less per point than shared buffers — and costs far ' +
    'more if the estimate is wrong.',

  maintenance_mem_part:
    'Share of available RAM for maintenance work: VACUUM, ANALYZE, CREATE INDEX ' +
    'and autovacuum.\n' +
    'Divided between autovacuum workers and manual maintenance sessions by the ' +
    'two parts below.',

  autovacuum_workers_mem_part:
    'How much of the maintenance budget goes to autovacuum workers, sizing ' +
    'autovacuum_work_mem.\n' +
    'Must add up to exactly 1.0 with maintenance-conns-mem-part; moving one ' +
    'moves the other.',

  maintenance_conns_mem_part:
    'How much of the maintenance budget goes to manual maintenance sessions, ' +
    'sizing maintenance_work_mem.\n' +
    'Must add up to exactly 1.0 with autovacuum-workers-mem-part; moving one ' +
    'moves the other.',

  work_mem_concurrency_factor:
    'How many work_mem-sized allocations one session is assumed to hold at ' +
    'once — several operators in one plan, parallel workers under them, and ' +
    'hash operations with their own multiplier.\n' +
    'Raising it shrinks work_mem and makes the memory envelope more ' +
    'conservative. Never below 1. pg_diag can tell you what the real figure is.',

  peak_wal_rate:
    'WAL written per second at peak, with an IEC suffix.\n' +
    'Sizes max_wal_size — peak rate times the checkpoint interval, doubled — and ' +
    'how much WAL is kept for a replica that has fallen behind. Worth measuring ' +
    'rather than guessing; it is the input the WAL settings are most sensitive to.',

  replica_outage_tolerance:
    'How long, in seconds, a replica may be absent and still catch up from ' +
    'retained WAL instead of needing a fresh base backup.\n' +
    'Multiplied by peak-wal-rate to get the retention target, which is then ' +
    'capped at 40% of the WAL disk budget.',

  wal_disk_budget:
    'Total disk the pg_wal directory may occupy, retained WAL included.\n' +
    'It caps max_wal_size at half the budget and retention at 40% of it, and it ' +
    'must be large enough for at least eight WAL segments.',

  wal_segment_size:
    'The segment size this cluster was created with.\n' +
    'Fixed by initdb and unchangeable afterwards, and never written to ' +
    'postgresql.conf — it is asked for, not set. WAL sizing has to land on whole ' +
    'segments, and on PostgreSQL 12 and earlier wal_keep_segments counts ' +
    'segments rather than bytes, so the number has to be the true one.',

  min_conns:
    'Floor for the computed max_connections.\n' +
    'Raise it when a pool or an application demands a minimum regardless of how ' +
    'small the hardware is.',

  max_conns:
    'Ceiling for the computed max_connections.\n' +
    'A connection costs memory whether or not it is running a query, so this is ' +
    'a memory decision as much as a capacity one. A pooler in front of the ' +
    'database is what lets it stay low.',

  min_autovac_workers:
    'Floor for autovacuum_max_workers, whatever the core count works out to.\n' +
    'Worth raising on a database with many small, frequently updated tables, ' +
    'where what limits cleanup is how many tables can be vacuumed at once rather ' +
    'than how fast one vacuum runs.',

  max_autovac_workers:
    'Ceiling for autovacuum_max_workers.\n' +
    'More workers clear bloat faster, but each one holds autovacuum_work_mem and ' +
    'competes for the same I/O the queries need.',

  min_maint_conns:
    'Floor for the number of maintenance sessions assumed to run at the same ' +
    'time when maintenance_work_mem is sized.',

  max_maint_conns:
    'Ceiling for the same assumption.\n' +
    'maintenance_work_mem is sized so that this many sessions can each hold it ' +
    'at once, so raising the ceiling makes every one of them smaller.',

  platform:
    'The operating system the cluster runs on.\n' +
    'Windows exposes neither TCP_KEEPCNT nor TCP_USER_TIMEOUT and does no client ' +
    'connection polling, so those settings are left at 0 and a warning records ' +
    'why.',

  conf_profiles:
    'Optional rule sets applied on top of the base configuration, in the order ' +
    'listed here.\n' +
    'Where two profiles set the same parameter the later one wins, so the order ' +
    'is part of the answer, not a detail. Each profile also declares the ' +
    'extensions it needs.',
};
