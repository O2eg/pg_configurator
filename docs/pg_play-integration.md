# pg_play integration contract

This document is for orchestrator authors. Normal users should use the public
`pg-configurator` options documented in the main README.

`pg_configurator` supports the hidden, versioned `pg_play/component/v2`
machine transport. Hidden flags do not appear in the primary `--help`:

```bash
pg-configurator --machine --request-id config-001 --component-capabilities
pg-configurator --machine --request-id config-002 \
  --input-json=- --validate-input
pg-configurator --machine --request-id config-003 \
  --input-json=- --output-format=json
```

The capability document uses `pg_play/capabilities/v1`. Every command declares
the common boolean fields `mutates_target`, `machine_output`, and
`accepts_plan_hash`. The older hidden `--capabilities` spelling remains an
alias for compatibility.
Its `machine_interface` object records these three canonical hidden option names.

Standard input for the last two calls is:

```json
{
  "schema_version": "pg_configurator/input-v1",
  "inputs": {
    "db_cpu": 4,
    "db_ram": "8Gi",
    "pg_version": "18",
    "db_duty": "mixed"
  }
}
```

Explicit CLI values override fields from JSON. Unknown fields and invalid
types are errors. The generated `pg_configurator/v2` artifact has an
`artifact_hash` calculated without volatile generation timestamps. Patroni
output includes the exact applicable document in `result.document`.

The envelope field is `advisories`, not `warnings`, and its items are objects
rather than strings — that is what separates `pg_play/component/v2` from `v1`:

```json
{
  "code": "wal_retention_capped",
  "severity": "warning",
  "setting": "wal_keep_segments",
  "actual": "2",
  "message": "Keeping 4800MB of WAL for a 300s outage would need more than ..."
}
```

`severity` is one of three. `warning` is a real risk or a conflict in the
result. `assumption` is a premise the calculation rests on and could not check.
`info` is a boundary of what the tool does, or an explanation of a choice it
made. A `code` is stable across releases and is the field to route on; the
`message` is written for a person and is not. Every advisory is built from the
finished configuration, so `actual` is always the value the emitted file
carries, and the ordering is severest first.

The component does not accept credentials and never mutates PostgreSQL.
