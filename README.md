# pg_configurator

- Converts hardware, workload, PostgreSQL major, topology, and storage limits
  into one reproducible configuration candidate instead of an undocumented set
  of DBA guesses.
- Keeps memory, connections, workers, locks, WAL, autovacuum, network timeouts,
  logging, and observability inside explicit safety budgets.
- Produces an explainable JSON artifact: every GUC contains its source, rule
  kind, raw value, context provenance, and minimum deployment action.
- Validates every core setting against a real `pg_settings` snapshot for the
  selected major and boot-tests generated base configurations on PostgreSQL
  9.6–18.
- Gives [pg_play](https://github.com/O2eg/pg_play) and
  [pg_stand](https://github.com/O2eg/pg_stand) a
  deterministic configuration input so the
  same stand can be rebuilt, loaded by `pg_workload`, inspected by `pg_diag`,
  and compared with another candidate.

`pg_configurator` generates an explainable PostgreSQL configuration candidate
from hardware limits, database duty, replication requirements, PostgreSQL
version, and optional workload profiles.

The algorithm is empirical. Its output is a candidate for an isolated test,
not a configuration that should be applied to production without validation.

Supported PostgreSQL versions: 9.6 and 10–18. PostgreSQL 9.6–13 are retained
only for legacy stands and tests because the server majors are end-of-life;
the generated artifact repeats that warning. The intended Python baseline is
3.10–3.12.

## How it works

The calculation remains one cohesive operation, but the internal data flow is
explicit:

```text
CPU  RAM  disk  duty  PG major  topology  platform  extension inventory
 |    |     |     |       |         |         |              |
 +----+-----+-----+-------+---------+---------+--------------+
                              |
                    normalize and validate
                              |
        +---------------------+----------------------+
        |          |          |         |            |
        v          v          v         v            v
     memory    workers     locks       WAL      network/timeouts
     budget     budget     budget     budget       and security
        |          |          |         |            |
        +---------------------+----------------------+
                              |
              base rules -> workload profiles
                    -> mandatory conf_common
                              |
                   PG-major snapshot validation
                 + cross-parameter safety invariants
                              |
              +---------------+----------------+
              |               |                |
              v               v                v
       postgresql.conf   Patroni JSON   pg_configurator/v1 JSON
```

## Installation

From PyPI:

```bash
python -m pip install pg-configurator
```

For development:

```bash
git clone https://github.com/O2eg/pg_configurator.git
cd pg_configurator
python -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
```

## Command examples

Show help or the installed package version:

```bash
pg-configurator --help
pg-configurator --version
python -m pg_configurator --version
```

Generate `postgresql.conf` output:

```bash
pg-configurator \
  --db-cpu=16 \
  --db-ram=64Gi \
  --db-size=500Gi \
  --db-disk-type=SSD \
  --db-duty=mixed \
  --replication-mode=physical \
  --pg-version=18 \
  --available-extensions=pg_stat_statements,auto_explain
```

Generate the machine-readable artifact on stdout or write it directly to a
file:

```bash
pg-configurator \
  --db-cpu=16 \
  --db-ram=64Gi \
  --pg-version=18 \
  --output-format=json > candidate.json

pg-configurator \
  --db-cpu=16 \
  --db-ram=64Gi \
  --pg-version=18 \
  --output-format=json \
  --out=candidate.json
```

Generate Patroni parameter JSON:

```bash
pg-configurator \
  --db-cpu=16 \
  --db-ram=64Gi \
  --pg-version=18 \
  --output-format=patroni-json
```

Generate a disposable stand configuration with neither replication nor PITR:

```bash
pg-configurator \
  --db-cpu=4 \
  --db-ram=8Gi \
  --pg-version=18 \
  --replication-mode=none \
  --pitr-enabled=false
```

Generate a financial candidate with truthful synchronous durability. The
named standby is required before `remote_apply` is emitted:

```bash
pg-configurator \
  --db-cpu=32 \
  --db-ram=128Gi \
  --db-duty=financial \
  --pg-version=18 \
  --replication-mode=physical \
  --replica-count=2 \
  --synchronous-standby-names='ANY 1 (standby1,standby2)'
```

Generate a high-concurrency OLTP candidate. It keeps transaction durability,
uses a 32 MiB `work_mem` ceiling, gives connection capacity more CPU weight,
limits query parallelism, and runs autovacuum more frequently:

```bash
pg-configurator \
  --db-cpu=32 \
  --db-ram=128Gi \
  --db-duty=oltp \
  --pg-version=18 \
  --replication-mode=physical
```

Generate a logical-replication candidate. Native subscription counts are
accepted only on PostgreSQL 10+:

```bash
pg-configurator \
  --db-cpu=24 \
  --db-ram=96Gi \
  --pg-version=18 \
  --replication-mode=logical \
  --replica-count=1 \
  --logical-subscription-count=3
```

Use measured storage and explicit WAL capacity inputs:

```bash
pg-configurator \
  --db-cpu=48 \
  --db-ram=256Gi \
  --pg-version=18 \
  --db-disk-type=NVME \
  --disk-score=88 \
  --peak-wal-rate=64Mi \
  --replica-outage-tolerance=1800 \
  --wal-disk-budget=256Gi \
  --wal-segment-size=16Mi
```

Override the default memory envelope only when the complete sum is understood:

```bash
pg-configurator \
  --db-cpu=32 \
  --db-ram=128Gi \
  --pg-version=18 \
  --shared-buffers-part=0.30 \
  --client-mem-part=0.20 \
  --maintenance-mem-part=0.10 \
  --autovacuum-workers-mem-part=0.60 \
  --maintenance-conns-mem-part=0.40 \
  --work-mem-concurrency-factor=6
```

Select a target OS. On Windows, unsupported Linux socket options remain at the
operating-system default:

```bash
pg-configurator \
  --db-cpu=16 \
  --db-ram=64Gi \
  --pg-version=18 \
  --platform=WINDOWS
```

Show normalized inputs and evaluated rule values on stderr while keeping
stdout usable for the generated configuration:

```bash
pg-configurator --db-cpu=8 --db-ram=32Gi --pg-version=18 --debug
```

The supported boolean values are `true`, `false`, `1`, `0`, `yes`, `no`,
`on`, and `off`.

## CLI reference

Defaults shown below are package defaults. CPU and RAM are detected from the
machine running the command when their options are omitted.

### General and output

| Parameter | Default | Description |
| --- | --- | --- |
| `-h`, `--help` | — | Print the complete argparse help and exit. |
| `--version` | `false` | Print the package version and exit. |
| `--debug` | `false` | Print normalized inputs, rule sources, expressions, raw values, and formatted values to stderr. |
| `--output-format=conf\|json\|patroni-json` | `conf` | Select plain `postgresql.conf`, the full versioned artifact, or Patroni parameter JSON. |
| `--out=PATH` | stdout | Write output to a file; diagnostics still go to stderr. An existing file is first preserved as `PATH.<timestamp>.bak`. `--output-file-name` remains a compatibility alias. |

### Target and workload

| Parameter | Default | Description |
| --- | --- | --- |
| `--db-cpu=CORES` | detected | Available CPU cores. Decimal cores and Kubernetes-style millicores such as `500m` are accepted. |
| `--db-ram=SIZE` | detected | Physical RAM using IEC units such as `Mi`, `Gi`, or `Ti`. |
| `--db-size=SIZE` | unknown | Optional logical database size. It selects a bounded `default_statistics_target` tier; omitting it uses only duty and resource capacity. |
| `--db-disk-type=SATA\|SAS\|SSD\|NVME\|NETWORK` | `SAS` | Fallback storage class used only when no measured score is supplied. |
| `--disk-score=0..100` | inferred | Measured composite storage score; one score consistently drives planner, I/O, and vacuum cost. |
| `--db-duty=financial\|oltp\|mixed\|statistic` | `mixed` | Workload composition used for connections, memory, autovacuum, timeouts, checkpoints, parallelism, logging, and network baselines. |
| `--pg-version=9.6\|10..18` | `18` | Target PostgreSQL major and rule/snapshot compatibility contract. |
| `--platform=LINUX\|WINDOWS` | `LINUX` | Target OS; controls which TCP socket features can be emitted. |

### Replication, PITR, and WAL

| Parameter | Default | Description |
| --- | --- | --- |
| `--replication-mode=none\|physical\|logical` | `physical` | Explicit capability. `logical` also preserves physical-replication capability. |
| `--replication-enabled=BOOL` | unset | Compatibility alias: `true` maps to `physical` and `false` to `none`. When both replication options are explicit, they must agree. |
| `--pitr-enabled=BOOL` | `true` | Keeps `wal_level` PITR-compatible. It does not fabricate backup storage or `archive_command`. |
| `--synchronous-standby-names=VALUE` | empty | Exact PostgreSQL value. A non-empty value is required before financial duty can emit `remote_apply`. |
| `--replica-count=N` | `1` | Expected physical replicas used to reserve senders and slots. Ignored when replication mode is `none`. |
| `--logical-subscription-count=N` | `0` | Native subscriptions used for logical-worker and decoding-memory budgets; requires logical mode and PostgreSQL 10+. |
| `--peak-wal-rate=SIZE` | `4Mi` | Expected peak WAL bytes per second. |
| `--replica-outage-tolerance=SECONDS` | `900` | How long a replica may be disconnected while retained WAL remains available. |
| `--wal-disk-budget=SIZE` | `32Gi` | Total `pg_wal`/retention capacity; must be at least 1 GiB and hold at least eight WAL segments. |
| `--wal-segment-size=SIZE` | `16Mi` | Actual cluster segment size, a power of two from 1 MiB through 1 GiB. |

### Memory, connections, and workers

| Parameter | Default | Description |
| --- | --- | --- |
| `--reserved-ram-percent=PERCENT` | `10` | Percentage removed from physical RAM before PostgreSQL budgets are calculated. |
| `--reserved-system-ram=SIZE` | `256Mi` | Additional fixed OS/runtime reserve removed after the percentage. |
| `--shared-buffers-part=FRACTION` | `0.25` | Available-RAM fraction for `shared_buffers`. |
| `--client-mem-part=FRACTION` | `0.20` | Fraction for concurrent `work_mem` and `temp_buffers` envelopes. |
| `--maintenance-mem-part=FRACTION` | `0.10` | Fraction shared by maintenance sessions and autovacuum workers. |
| `--autovacuum-workers-mem-part=FRACTION` | `0.50` | Share of the maintenance budget assigned to all possible autovacuum workers. |
| `--maintenance-conns-mem-part=FRACTION` | `0.50` | Share of the maintenance budget assigned to concurrent manual maintenance. |
| `--work-mem-concurrency-factor=NUMBER` | `4.0` | Per-active-session amplification for simultaneous operators, parallel work, and hashes; must be at least 1. |
| `--min-conns=N`, `--max-conns=N` | `20`, `500` | Bounds for calculated client connections; backend memory and CPU can lower the result. |
| `--min-autovac-workers=N`, `--max-autovac-workers=N` | `3`, `20` | Bounds for CPU-scaled autovacuum workers. |
| `--min-maint-conns=N`, `--max-maint-conns=N` | `4`, `16` | Assumed range of concurrent maintenance sessions used for memory budgeting. |

Each main memory fraction must be in `(0, 0.4]`; the three main fractions may
sum to at most `0.75`. The two maintenance sub-fractions must sum to exactly
`1.0`. Generation fails when the final shared/client/maintenance/logical/lock/
backend envelope exceeds 90% of available RAM.

### Profiles, extensions, and history

| Parameter | Default | Description |
| --- | --- | --- |
| `--conf-profiles=LIST` | empty | Ordered comma-separated workload profile list; duplicates, unknown names, and incompatible combinations are rejected. |
| `--available-extensions=LIST` | unverified | Caller-declared extension inventory for this target major. Missing required names become an error; no live server check is performed. |
| `--common-conf` | `true` | Mandatory version-aware logging/observability contract. |
| `--no-common-conf` | rejected | Legacy spelling retained to fail explicitly rather than silently omit required safety settings. |
| `--settings-history=OLD,NEW` | empty | Return a JSON comparison of two bundled `pg_settings` snapshots. |
| `--specific-setting-history=NAME` | empty | Return one GUC's presence/default history across every supported major. |

## Artifact contract

JSON output uses `schema_version: pg_configurator/v1` and contains:

- normalized, typed inputs;
- independent CPU, RAM, storage, connection, memory, worker, lock, WAL, and
  network-timeout budgets;
- formatted PostgreSQL values and typed raw values;
- the winning source, `constant` or `expression` rule kind, and expression when
  one exists; constant rules deliberately carry `rule: null` because their
  typed value is already recorded as `raw_value`;
- `pg_settings.context` plus `context_source=pg_settings_snapshot` for core and
  contrib GUCs; external extension GUCs without captured metadata use
  `context=unknown` and `context_source=external_extension`;
- a minimum deployment action derived from the context: `restart`, `reload`,
  `reload_and_reconnect`, `immutable`, or `manual`;
- profile override history;
- required extension metadata and whether availability was merely declared by
  the caller;
- warnings and unverified assumptions;
- a flat `postgresql_conf` mapping for consumers that only need GUC values.

Diagnostic messages are written to stderr. Stdout remains a valid JSON document
when `--output-format=json` is selected.

The context/action mapping is:

| PostgreSQL context | Artifact `apply_mode` |
| --- | --- |
| `postmaster` | `restart` |
| `sighup`, `user`, `superuser` | `reload` |
| `backend`, `superuser-backend` | `reload_and_reconnect` |
| `internal` | `immutable` |
| unknown external-extension context | `manual` |

`reload` changes the configuration-source default; it does not erase a value
already overridden in a session, role, or database.
`reload_and_reconnect` means the server must reread the configuration and only
subsequently opened sessions receive the new value.

## Resource and workload model

The default available-memory budgets are:

```text
shared_buffers:            25%
concurrent query memory:   20%
maintenance + autovacuum: 10%
OS, backends and reserves: 45%
```

The three configurable budgets may use at most 75% of available RAM. This
preserves memory for PostgreSQL processes, extensions, WAL and lock structures,
the operating system, and its filesystem cache. `effective_cache_size` includes
`shared_buffers` and only the OS-cache estimate left after concurrent query,
maintenance, backend, lock, and logical-decoding reserves are subtracted.

Connection capacity reserves 10 MiB of non-query memory per backend and can use
at most 67% of the otherwise unassigned headroom; the remainder is retained for
locks, logical decoding, WAL, extension workers, kernel buffers, and estimation
error. It is also bounded by CPU. `work_mem` budgets
`min(max_connections, max(4, 2 × floor(CPU)))` simultaneously active query
sessions and is divided by
`--work-mem-concurrency-factor` (default `4.0`) and `hash_mem_multiplier`.
`temp_buffers` is charged to the same concurrent client budget. The artifact
records the complete concurrent-memory envelope and generation fails if it
exceeds 90% of available RAM.

`maintenance_work_mem` and all possible autovacuum workers share a separate
maintenance budget. `logical_decoding_work_mem` is capped per configured
replication-slot capacity. PG17 SLRU buffers remain on PostgreSQL's automatic
defaults instead of consuming up to several GiB of unbudgeted shared memory.

### Duty composition

`--db-duty` selects a coordinated set of bounded calculations; it is not a
label applied after generation. The connection target starts with
`min_conns + (CPU - 1) × CPU increment` and is then constrained by RAM and
`--max-conns`.

| Duty | Intended workload | CPU connection increment | `work_mem` ceiling | Parallel CPU share | Checkpoint | Autovacuum naptime / vacuum / analyze scale |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| `financial` | critical short transactions | 4 | 16 MiB | 25% | 5 min | 30 s / 0.02 / 0.01 |
| `oltp` | high-concurrency short read/write transactions | 5 | 32 MiB | 35% | 10 min | 20 s / 0.015 / 0.0075 |
| `mixed` | transactional and analytical queries | 4 | 64 MiB | 50% | 15 min | 30 s / 0.02 / 0.01 |
| `statistic` | aggregations and long analytical queries | 4 | 256 MiB | 75% | 30 min | 30 s / 0.02 / 0.01 |

OLTP also uses `hash_mem_multiplier=1.5`, at most two parallel workers per
query or maintenance command, more CPU-scaled autovacuum workers, and
`synchronous_commit=on`. Financial remains the only duty that can promote
durability to `remote_apply` when synchronous standbys are explicitly named.

### Planner statistics

`default_statistics_target` is selected from `500`, `1000`, `2500`, and
`5000`. It is never increased only because a host is powerful: the duty and
optional `--db-size` first produce a candidate, then CPU and available RAM cap
the result.

| Duty | Base | Maximum |
| --- | ---: | ---: |
| `financial` | 500 | 1000 |
| `oltp` | 500 | 2500 |
| `mixed` | 1000 | 5000 |
| `statistic` | 2500 | 5000 |

Database-size multipliers are `1` when size is unknown or below 100 GiB, `2`
from 100 GiB to below 1 TiB, and `4` from 1 TiB. CPU caps are 500/1000/2500/
5000 at 4/8/16-core boundaries; RAM uses the same caps at 8/32/128-GiB
boundaries. The lower resource cap wins. `profile_1c` starts at 1000, while
`profile_backend_perf` keeps at least 500 but still respects the same resource
ceiling.

The database-size tier is deliberately only a coarse proxy for schema and data
distribution complexity. PostgreSQL's sample size is controlled by the target
and does not need to grow linearly with every table. Use `pg_diag` estimation
errors to move exceptional skewed columns to `ALTER TABLE ... SET STATISTICS`
instead of raising the cluster-wide value indefinitely. PostgreSQL documents
the accuracy versus `ANALYZE` cost trade-off in
[Query Planning](https://www.postgresql.org/docs/18/runtime-config-query.html).

### Parallel planning, JIT, and execution I/O

The planner cost model uses the same duty that limits worker counts:

| Duty | `parallel_setup_cost` | `parallel_tuple_cost` | table threshold | index threshold |
| --- | ---: | ---: | ---: | ---: |
| `financial` | 2000 | 0.15 | 32 MiB | 2 MiB |
| `oltp` | 1500 | 0.12 | 16 MiB | 1 MiB |
| `mixed` | 1000 | 0.10 | 8 MiB | 512 KiB |
| `statistic` | 500 | 0.05 | 4 MiB | 256 KiB |

PostgreSQL 9.6 receives `min_parallel_relation_size`; PostgreSQL 10+ receives
the split table/index thresholds. PostgreSQL 11+ disables JIT for `financial`
and `oltp`, keeps the upstream 100000/500000 cost boundaries for `mixed`, and
uses 50000/250000 for `statistic`. `profile_1c` always overrides JIT to `off`.
JIT primarily helps long-running CPU-bound queries and can cost more than it
saves for short statements; see PostgreSQL's
[JIT decision guidance](https://www.postgresql.org/docs/18/jit-decision.html).

PostgreSQL 16+ receives `vacuum_buffer_usage_limit`, bounded by one eighth of
`shared_buffers`: 1/2/8/32 MiB for financial/OLTP/mixed/statistic, with a 2 MiB
ceiling for 1C. PostgreSQL 17+ receives `io_combine_limit`; PG17 is capped at
256 KiB. PG18 can use up to 1 MiB on Linux for fast analytical storage, while
Windows remains capped at 128 KiB. PG18 also receives a matching
`io_max_combine_limit`.

## Storage model

Built-in storage classes are `SATA`, `SAS`, `SSD`, `NVME`, and `NETWORK`. Each
maps to one fallback score. When `--disk-score` is supplied, it consistently
drives planner cost, query I/O concurrency, maintenance I/O and autovacuum cost;
`disk_type` no longer produces contradictory values.

```bash
pg-configurator ... --disk-score=82
```

The score should reflect observed latency, IOPS, RAID/cache behavior, network
storage, and the complete data/WAL topology. PG18 keeps
`io_max_concurrency=-1`, allowing PostgreSQL to calculate a safe process-aware
limit, while `io_workers` scales conservatively with CPU.

## Replication, durability, and WAL

Use an explicit replication capability:

```text
none      no streaming or logical replication
physical  physical streaming replication
logical   logical plus physical replication capability
```

`--replication-enabled=true|false` remains as a compatibility alias mapping to
`physical|none`. When it is omitted, an explicit `--replication-mode` wins; if
both options are explicitly supplied, contradictory values are rejected.
`--pitr-enabled=true` is independent and keeps `wal_level` at least `replica`;
`minimal` is generated only when both replication and PITR are disabled.

`fsync` and `full_page_writes` are always enabled. Every workload duty uses
`synchronous_commit=on`; the calculator never trades acknowledged transactions
for speed implicitly. Financial duty uses `remote_apply` only when
`--synchronous-standby-names` is provided. The worker-process budget accounts
for parallel queries, logical replication, extension workers, and non-parallel
background headroom; parallel maintenance shares the parallel-worker pool.

`--pitr-enabled` preserves the WAL level needed by a PITR design. It deliberately
does not invent an `archive_command` or backup destination: credentials,
retention, object storage, and archive transport belong to
[pg_stand](https://github.com/O2eg/pg_stand). The artifact warns about this
deployment boundary so WAL-level capability is
not mistaken for a working backup chain.

WAL retention uses the same bounded byte target on all major versions when
replication is enabled:

```text
desired retention = peak WAL bytes/second × tolerated replica outage
retention = min(max(desired retention, 512 MiB), 40% of WAL disk budget)

max_wal_size = min(
    max(peak WAL rate × checkpoint interval × 2, 1 GiB, 4 segments),
    50% of WAL disk budget
)
min_wal_size = min(max(max_wal_size / 4, 2 segments), 4 GiB)
```

Configure it with `--peak-wal-rate`, `--replica-outage-tolerance`,
`--wal-disk-budget`, and the actual `--wal-segment-size`. The disk budget must
be at least 1 GiB and hold at least eight segments. Because the disk-budget cap
is applied last, an unusually small valid budget can reduce retention below the
512 MiB preferred floor. PG13+ receives `max_slot_wal_keep_size`, and PG18 also
receives `idle_replication_slot_timeout`. With replication disabled, retained
WAL and replication slot/sender budgets are zero.

Autovacuum table thresholds and scale factors are deliberately conservative
and independent of host size; OLTP uses a more responsive composition for
write-heavy tables. Worker count follows CPU and duty; cost limit/delay follow
the same storage score used by planner and I/O settings. PG18 uses
`autovacuum_vacuum_max_threshold`. Table-specific scale factors should be
applied from observed table sizes and churn rather than from hardware scores.

## Locks, timeouts, and parallelism

Parallel-query, parallel-maintenance, logical-replication, autovacuum, and
extension workers are accounted for as separate consumers of CPU/RAM-derived
budgets. Parallel workers are constrained by both the workload duty and
available memory; the worker-process total reserves room for non-parallel
background workers.
Parallel query and maintenance are disabled below two effective CPU cores or
2 GiB of available RAM.

The four lock-capacity parameters scale in bounded tiers as RAM and connection
capacity grow: `max_locks_per_transaction`,
`max_pred_locks_per_transaction`, `max_pred_locks_per_page`, and
`max_pred_locks_per_relation`. Their estimated shared-memory use is included in
the memory envelope rather than treated as free.

Every profile inherits conservative `lock_timeout`, `statement_timeout`, and
`idle_in_transaction_session_timeout` values. PostgreSQL 14+ also receives
`idle_session_timeout`, and PostgreSQL 17+ receives `transaction_timeout`.
Financial and OLTP duties use shorter limits; analytical duty permits longer
queries without allowing abandoned sessions or transactions to live forever.

These values are intentionally present in the generated stand candidate, but
PostgreSQL recommends against short instance-wide query and transaction
timeouts because they also affect maintenance and administrative sessions. For
a production deployment, move the same workload defaults to `ALTER ROLE` or
`ALTER DATABASE`, keep a less restrictive DBA role, and verify
`idle_session_timeout` against the connection pooler's reconnect behaviour.

## Network, connection, and timeout model

Network settings are derived from workload duty and target platform; they are
not derived from CPU or RAM. The Linux failure window follows the relationship
recommended by the PostgreSQL operations guidance:

```text
idle TCP connection
        |
        | tcp_keepalives_idle
        v
 first keepalive probe
        |
        | tcp_keepalives_interval × tcp_keepalives_count
        v
 dead connection detected

 expected detection window = idle + interval × count
 tcp_user_timeout          = expected detection window

 long-running query ----------------> client_connection_check_interval (PG14+)
 replication stream ---------------> wal_sender/receiver_timeout
 authentication handshake ----------> authentication_timeout
 transaction lock graph ------------> deadlock_timeout + lock_timeout
```

Linux baselines:

| Duty | Keepalive idle | Interval | Count | Failure window | PG14+ client check | Replication timeout |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `financial` | 60 s | 10 s | 6 | 120 s | 5 s | 60 s |
| `oltp` | 90 s | 15 s | 6 | 180 s | 10 s | 90 s |
| `mixed` | 120 s | 30 s | 4 | 240 s | 10 s | 120 s |
| `statistic` | 300 s | 30 s | 3 | 390 s | 30 s | 300 s |

`authentication_timeout` is 30 seconds. `deadlock_timeout` is 1 second for
financial, OLTP, and mixed workloads and 2 seconds for analytical workloads.
Application timeouts remain finite but deliberately longer than the fast
network checks:

| Duty | `lock_timeout` | `statement_timeout` | idle in transaction | PG14+ idle session | PG17+ transaction |
| --- | ---: | ---: | ---: | ---: | ---: |
| `financial` | 5 s | 5 min | 5 min | 4 h | 30 min |
| `oltp` | 10 s | 15 min | 10 min | 6 h | 1 h |
| `mixed` | 15 s | 30 min | 15 min | 8 h | 2 h |
| `statistic` | 1 min | 4 h | 1 h | 24 h | 8 h |

Version and platform handling is explicit:

- PostgreSQL 9.6+ receives TCP keepalive and authentication settings.
- PostgreSQL 10+ uses `scram-sha-256` for newly stored passwords.
- PostgreSQL 12+ receives `tcp_user_timeout` and a minimum TLS protocol of
  `TLSv1.2`.
- PostgreSQL 14+ receives `client_connection_check_interval`.
- PostgreSQL 16+ reserves a small additional connection pool for roles with
  `pg_use_reserved_connections` while preserving superuser slots.
- Windows supports keepalive idle/interval but not `TCP_KEEPCNT`,
  `TCP_USER_TIMEOUT`, or PostgreSQL's client socket polling. Those generated
  values remain `0`; the artifact records the limitation.
- `listen_addresses`, port, `pg_hba.conf`, certificates, DNS, firewall rules,
  load-balancer idle time, and client-driver connect/read timeouts are
  deployment facts and therefore belong to
  [pg_stand](https://github.com/O2eg/pg_stand), not this calculator.

The defaults are baselines, not universal network truth. Before apply, align
them with the proxy/firewall/client chain. The implementation follows the
[PostgreSQL connection-setting reference](https://www.postgresql.org/docs/current/runtime-config-connection.html),
the PostgreSQL wiki's
[timeout relationship](https://wiki.postgresql.org/wiki/Operations_cheat_sheet),
and AWS's operational guidance for
[dead connection handling](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/Appendix.PostgreSQL.CommonDBATasks.DeadConnectionHandling.html).

## Mandatory logging and observability

`conf_common.py` owns the complete logging and extension-observability layer.
It is mandatory: the legacy `--no-common-conf` spelling is retained only to
produce an explicit error instead of silently generating an incomplete
candidate.

The baseline enables the logging collector with `csvlog`, daily or 256 MiB
rotation, restrictive file permissions, bounded statement-duration thresholds,
sampled shorter statements, lock waits, checkpoints, slow autovacuum, and temp
files above 10 MiB. PostgreSQL cannot enforce a total log-directory size, so
the artifact always warns that external retention for `pg_log` is required.

`pg_stat_statements` and `auto_explain` are always preloaded. Expensive
`auto_explain` timing is disabled, only 1–5% of statements are sampled for
instrumentation depending on workload duty, the minimum duration is 5–30
seconds, and bind-parameter values are suppressed where the selected major
provides the relevant GUCs.
The generated rules follow actual major-version capabilities:

| PostgreSQL major | Additional rules first available in that major |
| --- | --- |
| 9.6–11 | baseline `auto_explain` and `pg_stat_statements` GUCs |
| 12 | `auto_explain.log_level`, `auto_explain.log_settings`, transaction sampling |
| 13 | `auto_explain.log_wal`, `pg_stat_statements.track_planning`, duration sampling |
| 14 | query ID, WAL I/O timing, recovery-conflict logging |
| 15 | inherits the PostgreSQL 14 capability set |
| 16 | `auto_explain.log_parameter_max_length` |
| 17–18 | inherit the PostgreSQL 16 extension capability set |

For every supported major, contrib GUC presence, type, enum values, bounds, and
apply context are validated against a snapshot captured after preloading both
modules. A contrib setting missing from that major's snapshot is an error, not
an unchecked custom GUC.

## Profiles and extension inventory

Every candidate already contains the base calculation and mandatory
`auto_explain`/`pg_stat_statements` configuration. Profiles only add or
override that baseline:

| Profile | Purpose | Additional extension names | Composition |
| --- | --- | --- | --- |
| `ext_perf` | More responsive autovacuum for write-intensive workloads | none | May be combined with backend profiles. |
| `profile_backend_common` | Backend compatibility plus temporary-table analysis and plan capture | `online_analyze`, `pg_store_plans` | May be combined with `ext_perf` and `profile_backend_perf`. |
| `profile_backend_perf` | Backend planner, statistics, huge-page, checkpoint-warning, and parallel-query overrides | none | May be combined with `ext_perf` and `profile_backend_common`. |
| `profile_1c` | Complete 1C compatibility baseline | `online_analyze`, `pg_store_plans`, `plantuner` | Exclusive; it must be the only profile. |

For composable profiles, rules are applied in list order and the last profile
wins when two profiles set the same GUC. Every replacement is recorded in the
artifact's `overrides` array. `profile_1c` is intentionally exclusive so its
JIT, parallel-query, compatibility, and `online_analyze` guarantees cannot be
silently undone by another profile.

Profiles are applied to a private rule copy for every call, so one experiment
cannot change the result of a later experiment in the same Python process.

Backend profile example with only mandatory contrib extensions:

```bash
pg-configurator \
  --db-cpu=16 \
  --db-ram=64Gi \
  --pg-version=18 \
  --conf-profiles=profile_backend_perf \
  --available-extensions=pg_stat_statements,auto_explain
```

1C compatibility profile with all required extension names declared by the
caller:

```bash
pg-configurator \
  --db-cpu=32 \
  --db-ram=128Gi \
  --db-size=2Ti \
  --pg-version=18 \
  --conf-profiles=profile_1c \
  --available-extensions=pg_stat_statements,pg_store_plans,auto_explain,plantuner,online_analyze
```

Extension dependency metadata and all extension GUC rules also live in
`conf_common.py`. `auto_explain` and `pg_stat_statements` are PostgreSQL contrib
modules; `online_analyze`, `pg_store_plans`, and `plantuner` are external and
are never assumed to be installed merely because rules exist for a PostgreSQL
major.

When `--available-extensions` is supplied, it is interpreted as a
caller-attested inventory for the selected target major and missing
dependencies are an error. The CLI does not connect to PostgreSQL, inspect
installed packages, load libraries, or read external-extension GUC metadata.
Declared names are marked `declared_available`, not `verified`. Without the
option, generation remains possible for offline planning, but every dependency
is marked `unverified` and a warning is emitted.

[pg_stand](https://github.com/O2eg/pg_stand) must build the inventory from the
actual target and must not apply a candidate until package availability,
library preloadability, and external GUC compatibility have been checked.

`profile_1c` follows the dedicated transactional baseline from the
[1C PostgreSQL tuning guidance](https://its.1c.ru/db/content/metod8dev/src/developers/scalability/administration/postgresql/i8105866.htm)
and the current
[Postgres Pro 1C guidance](https://postgrespro.ru/docs/enterprise/current/one-c-configuring-server):

- JIT and parallel query execution are always disabled; merge joins are
  disabled to match the session settings used by the 1C platform.
- `cpu_operator_cost=0.001`, both collapse limits are 20, GEQO remains enabled
  at 12 relations, and statistics use the 1C-specific bounded tier.
- `max_locks_per_transaction` stays between 512 and 2000;
  `max_connections` remains constrained by the caller's limit and the complete
  memory envelope.
- autovacuum starts from `max(4, ceil(floor(CPU)/4), --min-autovac-workers)`,
  is capped by `--max-autovac-workers`, and wakes every 20 seconds. The maximum
  bound must permit at least four workers.
- the background writer uses 20 ms / 4.0 / 400 as its delay, multiplier, and
  page limit; `max_files_per_process=8000` additionally requires `pg_stand` to
  verify the service's file-descriptor limit.
- `online_analyze` remains disabled by default; if explicitly enabled later,
  its temporary-table threshold baseline is 50 rows.

The profile intentionally requests compatibility settings including
`ssl=off`, `row_security=off`, and `standard_conforming_strings=off`; each is
reported as a warning. It does **not** adopt the performance-oriented
`synchronous_commit=off` recommendation because that can acknowledge recent
transactions before they are durable. Patched-distribution-only parameters
such as `enable_temp_memory_catalog` are not emitted into a vanilla PostgreSQL
artifact without a future explicit distribution capability input.

`profile_backend_perf` is an override-only profile; the base performance model
remains the single source for settings that are not backend-specific.

## Validation

Generation rejects:

- non-positive CPU or RAM;
- reserves that leave no RAM for PostgreSQL;
- invalid memory budgets or budgets using more than 75% of available RAM;
- `min` values greater than corresponding `max` values;
- unknown PostgreSQL versions, profiles, or incompatible profile combinations;
- unknown core GUCs and undeclared dotted extension GUCs;
- invalid boolean, enum, or numeric GUC syntax for the selected version;
- inconsistent replication mode, synchronous standby, or logical-subscription inputs;
- WAL budgets below 1 GiB or smaller than eight actual cluster segments;
- missing required extension names when a caller inventory is supplied;
- contrib extension GUCs absent from the selected major's captured snapshot;
- attempts to disable the mandatory common logging/observability contract;
- unsupported rule syntax or function calls.

Generation also enforces cross-parameter invariants for durability, PITR,
reserved connections, worker pools, parallelism, locks, WAL sizing, and the
concurrent-memory envelope.

## Settings history

Compare two bundled PostgreSQL snapshots:

```bash
pg-configurator --settings-history=16,18
```

Inspect one setting across every bundled version:

```bash
pg-configurator --specific-setting-history=max_parallel_maintenance_workers
```

Both commands return versioned JSON artifacts and use non-zero exit status for
invalid input.

Snapshots include `context`, `vartype`, bounds and enum values from real
`pg_settings`. Refresh them from installed official Docker images with:

```bash
python tools/refresh_pg_settings.py
```

## Python API

```python
from types import SimpleNamespace

from pg_configurator import PGConfigurator

args = SimpleNamespace(output_file_name="", debug_mode=False)
configurator = PGConfigurator(args, ext_params=[])
config = configurator.make_conf("16", "64Gi", pg_version="18")
artifact = configurator.build_artifact(config)
```

`PGConfiguratorResult` is an instance-based result object; results and warnings
are not shared between calls.

## Orchestrator integration

The normal CLI remains human-oriented. Authors of `pg_play`-compatible
orchestrators can use the separate [versioned machine contract](docs/pg_play-integration.md).

## Testing and release checks

Unit tests:

```bash
pytest -m 'not integration'
ruff check .
ruff format --check .
```

Docker integration tests are opt-in and require Docker plus PostgreSQL client
tools:

```bash
PG_CONFIGURATOR_DOCKER_INTEGRATION=1 pytest -m integration
```

For a local single-version smoke test, set for example
`PG_CONFIGURATOR_DOCKER_VERSIONS=18`. Release CI runs the complete version
matrix.

Build and inspect distributions:

```bash
python -m build
python -m twine check dist/*
```

Tagged releases are built and published through PyPI Trusted Publishing. A tag
must match the package version, for example `v0.9.1`.

## License and provenance

The project is distributed under the MIT License. Historical copyright and
repository provenance are documented in `LICENSE` and `NOTICE`.
