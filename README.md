# pg-configurator

`pg-configurator` generates an explainable PostgreSQL configuration candidate
from hardware limits, database duty, replication requirements, PostgreSQL
version, and optional workload profiles.

The algorithm is empirical. Its output is a candidate for an isolated test,
not a configuration that should be applied to production without validation.

```text
pg-configurator
      ↓ versioned config artifact
pg-stand → PostgreSQL
              ├─ pg-workload
              └─ pg-diag
                     ↓
             compare results
```

Supported PostgreSQL versions: 9.6 and 10–18. The intended ecosystem baseline
is Python 3.10–3.12.

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

## Basic usage

Generate `postgresql.conf` output:

```bash
pg-configurator \
  --db-cpu=16 \
  --db-ram=64Gi \
  --db-disk-type=SSD \
  --db-duty=mixed \
  --replication-enabled=true \
  --pg-version=18
```

Generate the machine-readable artifact:

```bash
pg-configurator \
  --db-cpu=16 \
  --db-ram=64Gi \
  --pg-version=18 \
  --output-format=json > candidate.json
```

Generate Patroni parameter JSON:

```bash
pg-configurator \
  --db-cpu=16 \
  --db-ram=64Gi \
  --pg-version=18 \
  --output-format=patroni-json
```

The supported boolean values are `true`, `false`, `1`, `0`, `yes`, `no`,
`on`, and `off`.

## Artifact contract

JSON output uses `schema_version: pg_configurator/v1` and contains:

- normalized, typed inputs;
- CPU, RAM, storage, and aggregate system scores;
- formatted PostgreSQL values and typed raw values;
- the winning source and rule for every parameter;
- `reload` or `restart` application mode;
- profile override history;
- warnings and unverified assumptions;
- a flat `postgresql_conf` mapping for consumers that only need GUC values.

Diagnostic messages are written to stderr. Stdout remains a valid JSON document
when `--output-format=json` is selected.

## Resource and workload model

The default available-memory partition is:

```text
shared_buffers: 70%
client memory:  20%
maintenance:    10%
```

The 70% `shared_buffers` default is retained from the original empirical model
and is intentionally reported as an aggressive-setting warning. Override the
three memory fractions when the operating-system cache or workload requires a
different balance.

`work_mem` is divided by `--work-mem-concurrency-factor` (default `2.0`). This
factor represents the combined amplification from concurrent sort/hash nodes,
parallel participants, and hash operations. It should be increased for query
profiles that can allocate several work areas at once.

The client-memory calculation never allocates less than PostgreSQL's minimum
`work_mem` and `temp_buffers` values. If the requested minimum connection count
cannot fit the client-memory budget, generation fails rather than silently
overcommitting memory.

## Storage model

Built-in storage classes are `SATA`, `SAS`, `SSD`, `NVME`, and `NETWORK`.
These classes are coarse fallbacks. For measured hardware, pass a composite
score between 0 and 100:

```bash
pg-configurator ... --disk-score=82
```

The score should reflect observed latency, IOPS, RAID/cache behavior, network
storage, and the complete data/WAL topology. The artifact records whether the
score was explicit or inferred from `--db-disk-type`.

PostgreSQL 18 candidates include `autovacuum_worker_slots` and the worker-based
asynchronous I/O settings `io_method`, `io_workers`, and `io_max_concurrency`.

## Profiles and extension preflight

Bundled profiles:

```text
ext_perf
profile_1c
profile_backend_common
profile_backend_perf
```

Profiles are applied to a private rule copy for every call, so one experiment
cannot change the result of a later experiment in the same Python process.

Example:

```bash
pg-configurator \
  --db-cpu=16 \
  --db-ram=64Gi \
  --pg-version=18 \
  --conf-profiles=profile_backend_perf,profile_1c \
  --available-extensions=pg_stat_statements,pg_store_plans,auto_explain,plantuner,online_analyze
```

When `--available-extensions` is supplied, missing profile dependencies are an
error. Without it, the artifact contains an explicit unverified-extension
warning.

`profile_1c` intentionally requests compatibility settings including
`ssl=off`, `row_security=off`, and `standard_conforming_strings=off`. Each is
reported as a warning. Review them before applying the candidate.

`profile_backend_perf` is an override-only profile; the base performance model
remains the single source for settings that are not backend-specific.

## Validation

Generation rejects:

- non-positive CPU or RAM;
- reserves that leave no RAM for PostgreSQL;
- invalid or non-positive memory partitions;
- `min` values greater than corresponding `max` values;
- unknown PostgreSQL versions or profiles;
- unknown core GUCs for the selected bundled `pg_settings` snapshot;
- missing extensions when extension preflight is requested;
- unsupported rule syntax or function calls.

CPU, RAM, system score, connections, and worker scaling is clamped to the
declared limits.

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
must match the package version, for example `v26.1.21`.

## License and provenance

The project is distributed under the MIT License. Historical copyright and
repository provenance are documented in `LICENSE` and `NOTICE`.
