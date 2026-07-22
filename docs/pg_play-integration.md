# pg_play integration contract

This document is for orchestrator authors. Normal users should use the public
`pg-configurator` options documented in the main README.

`pg_configurator` supports the hidden, versioned `pg_play/component/v1`
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
types are errors. The generated `pg_configurator/v1` artifact has an
`artifact_hash` calculated without volatile generation timestamps. Patroni
output includes the exact applicable document in `result.document`.

The component does not accept credentials and never mutates PostgreSQL.
