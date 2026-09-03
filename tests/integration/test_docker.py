"""Boot-smoke generated configurations in official PostgreSQL images.

The configuration is produced twice: by the Python reference and by the
JavaScript build. The differential suites already prove the two agree; this
proves the agreed answer actually starts a server, and it does so for whichever
producer generated it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from pg_configurator.configurator import PGConfigurator

ROOT = Path(__file__).resolve().parents[2]
JS_CLI = ROOT / "web" / "bin" / "pg-configurator.mjs"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("PG_CONFIGURATOR_DOCKER_INTEGRATION") != "1",
        reason="set PG_CONFIGURATOR_DOCKER_INTEGRATION=1 to run Docker integration tests",
    ),
]


ALL_VERSIONS = tuple(PGConfigurator.known_versions)


def requested_versions() -> tuple[str, ...]:
    requested = os.environ.get("PG_CONFIGURATOR_DOCKER_VERSIONS")
    if not requested:
        return ALL_VERSIONS
    selected = tuple(item.strip() for item in requested.split(",") if item.strip())
    unknown = sorted(set(selected) - set(ALL_VERSIONS))
    if unknown:
        raise ValueError(
            "Unknown versions in PG_CONFIGURATOR_DOCKER_VERSIONS: " + ", ".join(unknown)
        )
    return selected


def python_configuration(version: str, **overrides) -> dict[str, str]:
    configurator = PGConfigurator(
        SimpleNamespace(output_file_name="", debug_mode=False), ext_params=[]
    )
    return configurator.make_conf(
        "8",
        "16Gi",
        pg_version=version,
        replication_mode="physical",
        **overrides,
    )


def javascript_configuration(version: str, **overrides) -> dict[str, str]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    command = [
        node,
        str(JS_CLI),
        "--db-cpu=8",
        "--db-ram=16Gi",
        f"--pg-version={version}",
        "--replication-mode=physical",
        "--output-format=json",
    ]
    command.extend(f"--{name.replace('_', '-')}={value}" for name, value in overrides.items())
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)["postgresql_conf"]


PRODUCERS = {"python": python_configuration, "javascript": javascript_configuration}


def boot_configuration(version: str, config_path: Path, query: str) -> subprocess.CompletedProcess:
    script = " && ".join(
        (
            "initdb -D /tmp/pgdata -A trust >/dev/null",
            "cp /tmp/generated.conf /tmp/pgdata/postgresql.auto.conf",
            "pg_ctl -D /tmp/pgdata -o '-c listen_addresses=' -w start >/dev/null",
            f'psql -X -v ON_ERROR_STOP=1 -At -c "{query}"',
            "pg_ctl -D /tmp/pgdata -m fast -w stop >/dev/null",
        )
    )
    return subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--user",
            "postgres",
            "--entrypoint",
            "bash",
            "-v",
            f"{config_path}:/tmp/generated.conf:ro",
            f"postgres:{version}",
            "-c",
            script,
        ],
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("producer", sorted(PRODUCERS))
@pytest.mark.parametrize("version", requested_versions())
def test_generated_base_configuration_boots(version: str, producer: str, tmp_path: Path) -> None:
    config = PRODUCERS[producer](version)
    config_path = tmp_path / f"postgresql-{version.replace('.', '_')}-{producer}.conf"
    config_path.write_text(
        "\n".join(f"{name} = {value}" for name, value in config.items()) + "\n",
        encoding="utf-8",
    )

    result = boot_configuration(
        version,
        config_path,
        "SELECT current_setting('server_version'), "
        "current_setting('full_page_writes'), current_setting('fsync')",
    )

    assert result.returncode == 0, result.stderr
    assert "|on|on" in result.stdout


@pytest.mark.parametrize("producer", sorted(PRODUCERS))
def test_quoted_standby_name_is_one_safe_setting(producer: str, tmp_path: Path) -> None:
    standby_names = '"standby\'one\\west"'
    config = PRODUCERS[producer]("18", synchronous_standby_names=standby_names)
    config_path = tmp_path / f"postgresql-18-quoted-{producer}.conf"
    config_path.write_text(
        "\n".join(f"{name} = {value}" for name, value in config.items()) + "\n",
        encoding="utf-8",
    )

    result = boot_configuration(
        "18",
        config_path,
        "SELECT current_setting('synchronous_standby_names'), "
        "current_setting('full_page_writes'), current_setting('fsync')",
    )

    assert result.returncode == 0, result.stderr
    assert '"standby\'one\\west"|on|on' in result.stdout


def test_pg18_metadata_matches_runtime_context() -> None:
    configurator = PGConfigurator(
        SimpleNamespace(output_file_name="", debug_mode=False), ext_params=[]
    )
    configurator.make_conf("8", "16Gi", pg_version="18")

    assert configurator.last_parameter_details["io_workers"]["context"] == "sighup"
    assert configurator.last_parameter_details["io_max_concurrency"]["context"] == "postmaster"
    assert configurator.last_parameter_details["max_logical_replication_workers"]["context"] == (
        "postmaster"
    )
