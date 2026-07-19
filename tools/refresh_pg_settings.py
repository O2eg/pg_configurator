#!/usr/bin/env python3
"""Refresh bundled pg_settings snapshots from official PostgreSQL images."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

VERSIONS = ("9.6", "10", "11", "12", "13", "14", "15", "16", "17", "18")
QUERY = r"""
COPY (
    SELECT
        name,
        setting AS value,
        ''::text AS pretty_value,
        boot_val,
        COALESCE(unit, '') AS unit,
        context,
        vartype,
        min_val,
        max_val,
        COALESCE(enumvals::text, '') AS enumvals
    FROM pg_settings
    ORDER BY name
) TO STDOUT WITH (FORMAT CSV, HEADER true)
""".strip()


def snapshot_name(version: str) -> str:
    return f"settings_pg_{version.replace('.', '_')}.csv"


def fetch_snapshot(version: str) -> str:
    container_script = " && ".join(
        (
            "initdb -D /tmp/pgdata -A trust >/dev/null",
            (
                'printf "shared_preload_libraries = '
                "'pg_stat_statements,auto_explain'\\n\" >> /tmp/pgdata/postgresql.conf"
            ),
            "pg_ctl -D /tmp/pgdata -o '-c listen_addresses=' -w start >/dev/null",
            f"psql -X -v ON_ERROR_STOP=1 -At -c {subprocess.list2cmdline([QUERY])}",
        )
    )
    command = [
        "docker",
        "run",
        "--rm",
        "--user",
        "postgres",
        "--entrypoint",
        "bash",
        f"postgres:{version}",
        "-c",
        container_script,
    ]
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as error:
        raise RuntimeError(
            f"Could not read pg_settings from PostgreSQL {version}: {error.stderr.strip()}"
        ) from error
    if not result.stdout.startswith("name,value,pretty_value,boot_val,unit,context,vartype"):
        raise RuntimeError(f"Unexpected pg_settings output for PostgreSQL {version}")
    return result.stdout


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("versions", nargs="*", choices=VERSIONS)
    args = parser.parse_args()

    history_dir = Path(__file__).resolve().parents[1] / "pg_configurator" / "pg_settings_history"
    for version in args.versions or VERSIONS:
        target = history_dir / snapshot_name(version)
        target.write_text(fetch_snapshot(version), encoding="utf-8")
        print(f"refreshed PostgreSQL {version}: {target}")


if __name__ == "__main__":
    main()
