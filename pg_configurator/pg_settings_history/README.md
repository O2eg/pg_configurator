# Refreshing a PostgreSQL settings snapshot

The CSV files in this directory contain `pg_settings` snapshots used for
version compatibility checks and history reports. Generate a snapshot from an
unmodified official PostgreSQL container.

Example for PostgreSQL 18:

```bash
docker pull postgres:18
docker run --name pg-configurator-snapshot-18 \
  -v /tmp:/tmp \
  -e POSTGRES_PASSWORD=temporary-snapshot-container-only \
  -d postgres:18

docker exec -u postgres pg-configurator-snapshot-18 psql -c "COPY (
  SELECT
    name,
    setting AS value,
    CASE
      WHEN unit = '8kB' THEN pg_size_pretty(setting::bigint * 1024 * 8)
      WHEN unit = 'kB' AND setting <> '-1' THEN pg_size_pretty(setting::bigint * 1024)
      ELSE ''
    END AS pretty_value,
    boot_val,
    unit
  FROM pg_settings
  ORDER BY name
) TO '/tmp/settings_pg_18.csv' DELIMITER ',' CSV HEADER;"

docker rm -f -v pg-configurator-snapshot-18
```

Before replacing a bundled file, review the PostgreSQL image provenance and
diff the new snapshot. Never capture a production server configuration: the
files are intended to describe upstream defaults, not environment secrets.
