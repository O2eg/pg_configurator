# PostgreSQL settings snapshots

The CSV files in this directory describe upstream `pg_settings` defaults for
every supported PostgreSQL major version. Temporary snapshot clusters preload
the bundled `pg_stat_statements` and `auto_explain` modules, so their
version-specific GUCs are captured as well. Snapshots are used for
compatibility, type and enum validation, apply-mode calculation, and
settings-history reports.

Each snapshot contains:

- setting, boot value, and unit;
- `context` and `vartype`;
- numeric bounds;
- allowed enum values.

Refresh all snapshots from locally available official PostgreSQL Docker images:

```bash
python tools/refresh_pg_settings.py
```

Or refresh selected versions:

```bash
python tools/refresh_pg_settings.py 14 15 16 17 18
```

Review the image provenance and resulting diff before committing. The script
starts only temporary local clusters and never reads a production server, so
environment-specific settings and secrets cannot enter the package.
