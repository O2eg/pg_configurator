"""Differential parity on the command line itself.

Both implementations are driven with the same argv and their output compared.
This covers what the calculation-level suite cannot: argument parsing, type
coercion, argparse's abbreviation and alias behaviour, `--input-json`
expansion, the rendered `postgresql.conf`, the Patroni document, and the exit
code and error text of a rejected command line.

The rendered `.conf` includes the host name, so it is compared as text only
because both implementations read it from the same machine — the Node build
uses `os.hostname()` where Python uses `socket.gethostname()`, and a test below
asserts the two agree rather than assuming it.

The JSON artifact is compared field by field except for `generated_at`, which
is a timestamp, and `generator`/`schema_version`/`kind`/`artifact_hash`: the
JavaScript build reports itself as `pg-configurator-js` and emits a
`pg_configurator/preview-v1` document, because it does not reproduce the byte
encoding Python's canonical hash is defined over.
"""

import io
import json
import os
import shutil
import socket
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import psutil
import pytest

from pg_configurator.configurator import UnitConverter, run_pgc

ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "web" / "bin" / "pg-configurator.mjs"

BASE = ["--db-cpu=8", "--db-ram=32Gi"]

# Command lines both implementations must answer identically. They exercise the
# argparse behaviours a user relies on, not only the calculation.
ACCEPTED = [
    (["--pg-version=18"], "defaults"),
    (["--pg-version=9.6"], "oldest-major"),
    (["--pg-version=12", "--db-duty=oltp"], "duty"),
    (["--db-cpu", "16", "--db-ram", "64Gi", "--pg-version", "17"], "space-separated"),
    (["--db-c=4", "--db-r=16Gi", "--pg-v=16"], "abbreviated"),
    (["--pg-version=18", "--db-disk-type=NVME"], "enum-value"),
    (["--pg-version=18", "--disk-score=42.5"], "float"),
    (["--pg-version=18", "--replica-count=3"], "int"),
    (["--pg-version=18", "--pitr-enabled=false"], "parse-bool-false"),
    (["--pg-version=18", "--pitr-enabled", "no"], "parse-bool-word"),
    (["--pg-version=18", "--common-conf"], "boolean-optional-positive"),
    (["--pg-version=18", "--conf-profiles=profile_1c"], "profile"),
    (
        ["--pg-version=18", "--conf-profiles=profile_backend_common,profile_backend_perf"],
        "profiles-combined",
    ),
    (
        ["--pg-version=18", "--replication-mode=logical", "--logical-subscription-count=2"],
        "logical",
    ),
    (["--pg-version=18", "--replication-enabled=false"], "compat-alias"),
    (["--pg-version=18", "--platform=WINDOWS"], "windows"),
    (["--pg-version=17", "--platform=WINDOWS", "--db-disk-type=NVME"], "windows-prefetch"),
    (["--pg-version=18", "--peak-wal-rate=4Mi"], "peak-explicit"),
    (["--pg-version=18", "--synchronous-standby-names=s1"], "sync-single"),
    (["--pg-version=18", "--db-size=500Gi"], "db-size"),
    (["--pg-version=18", "--available-extensions=auto_explain,pg_stat_statements"], "extensions"),
    (["--pg-version=18", "--wal-segment-size=64Mi", "--wal-disk-budget=64Gi"], "wal"),
    (["--db-cpu=500m", "--db-ram=2Gi", "--pg-version=18", "--min-conns=5"], "millicores"),
]

REJECTED = [
    (["--pg-version=7.4"], "invalid-choice"),
    (["--db-disk-type=tape"], "invalid-enum"),
    (["--disk-score=abc"], "invalid-float"),
    (["--replica-count=x"], "invalid-int"),
    (["--pitr-enabled=maybe"], "invalid-bool"),
    (["--nonexistent=1"], "unrecognized"),
    (["--output-f=json"], "ambiguous"),
    (["--replica-count"], "missing-value"),
    (["--pg-version=18", "--conf-profiles=profile_1c,ext_perf"], "exclusive-profile"),
    (["--pg-version=18", "--min-conns=100", "--max-conns=50"], "range"),
    (["--pg-version=18", "--wal-disk-budget=512Mi"], "wal-budget"),
    (["--db-cpu=0", "--db-ram=8Gi"], "zero-cpu"),
    (["--pg-version=9.6", "--synchronous-standby-names=ANY 1 (a, b)"], "sync-any-on-96"),
    (
        ["--pg-version=18", "--synchronous-standby-names=standby1'\nfsync = off\n#"],
        "sync-conf-injection",
    ),
    (["--db-cpu=96", "--db-ram=8Gi"], "envelope"),
    (["--db-cpu=500m", "--db-ram=512Mi"], "reserve-below-min-conns"),
    # An option still waiting for a value never swallows the next option, known
    # or not; a flag never accepts one at all. Both used to pass here and change
    # the configuration silently.
    (["--synchronous-standby-names", "--pg-version=17"], "known-option-as-value"),
    (["--pg-version", "--db-duty=oltp"], "option-as-value-for-a-choice"),
    (["--replica-count", "--nonexistent"], "unknown-option-as-value"),
    (["--common-conf=false"], "boolean-optional-with-explicit-argument"),
    (["--no-common-conf=true"], "negated-flag-with-explicit-argument"),
    (["--debug=1"], "store-true-with-explicit-argument"),
    # A negative number is not an option, so it is still read as the value and
    # rejected by the range check rather than by the parser.
    (["--disk-score", "-5"], "negative-number-value"),
]

FORMATS = ("conf", "json", "patroni-json")


def python_cli(argv):
    """Run the reference CLI in-process and capture stdout, or its error."""

    stdout = io.StringIO()
    try:
        with redirect_stdout(stdout):
            run_pgc(list(argv))
    except (ValueError, TypeError) as error:
        return {"error": str(error), "exit": 2}
    except SystemExit as exit_error:  # argparse
        return {"error": None, "exit": int(exit_error.code or 0)}
    return {"stdout": stdout.getvalue(), "exit": 0}


def node_cli(argv, node):
    completed = subprocess.run([node, str(CLI), *argv], capture_output=True, text=True, check=False)
    return {
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "exit": completed.returncode,
    }


def strip_volatile(artifact):
    """Everything the two builds may legitimately disagree on.

    `schema_version` and `kind` are here because the JavaScript build emits a
    preview rather than a hashed artifact; `test_the_javascript_document_is_a
    _preview_rather_than_a_v2_artifact` pins what those two values must be.
    """
    trimmed = {key: value for key, value in artifact.items() if key != "generated_at"}
    for volatile in ("artifact_hash", "generator", "schema_version", "kind"):
        trimmed.pop(volatile, None)
    return trimmed


@pytest.mark.web
class TestCommandLineParity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if cls.node is None:
            if os.environ.get("PGC_REQUIRE_NODE"):
                raise AssertionError("node is required but was not found on PATH")
            raise unittest.SkipTest("node is not installed; skipping JavaScript parity")

    def test_rendered_conf_is_identical(self):
        for argv, label in ACCEPTED:
            with self.subTest(case=label):
                command = [*BASE, *argv] if not argv[0].startswith("--db-cpu") else list(argv)
                expected = python_cli(command)
                actual = node_cli(command, self.node)
                self.assertEqual(0, actual["exit"], actual["stderr"])
                self.assertEqual(expected["stdout"], actual["stdout"])

    def test_patroni_document_is_identical(self):
        for argv, label in ACCEPTED[:8]:
            with self.subTest(case=label):
                command = [*BASE, *argv, "--output-format=patroni-json"]
                expected = python_cli(command)
                actual = node_cli(command, self.node)
                self.assertEqual(0, actual["exit"], actual["stderr"])
                self.assertEqual(expected["stdout"], actual["stdout"])

    def test_patroni_document_recovers_raw_quoted_values(self):
        raw_value = r"standby'one\west"
        command = [
            *BASE,
            "--pg-version=18",
            f"--synchronous-standby-names={raw_value}",
            "--output-format=patroni-json",
        ]
        expected = python_cli(command)
        actual = node_cli(command, self.node)

        self.assertEqual(0, actual["exit"], actual["stderr"])
        self.assertEqual(expected["stdout"], actual["stdout"])
        document = json.loads(actual["stdout"])
        self.assertEqual(
            raw_value,
            document["postgresql"]["parameters"]["synchronous_standby_names"],
        )

    def test_json_artifact_matches_apart_from_its_provenance(self):
        for argv, label in ACCEPTED[:8]:
            with self.subTest(case=label):
                command = [*BASE, *argv, "--output-format=json"]
                expected = json.loads(python_cli(command)["stdout"])
                actual_run = node_cli(command, self.node)
                self.assertEqual(0, actual_run["exit"], actual_run["stderr"])
                actual = json.loads(actual_run["stdout"])
                self.assertEqual(strip_volatile(expected), strip_volatile(actual))

    def test_the_javascript_document_is_a_preview_rather_than_a_v2_artifact(self):
        # The canonical hash is defined over Python's json.dumps bytes, and this
        # build does not reproduce them: a Python float that happens to be
        # integral prints as `8.0` there and `8` here. Emitting the same content
        # is fine; calling it pg_configurator/v2 without the hash the schema
        # promises is what a consumer cannot defend against.
        command = [*BASE, "--pg-version=18", "--output-format=json"]
        expected = json.loads(python_cli(command)["stdout"])
        actual = json.loads(node_cli(command, self.node)["stdout"])

        self.assertEqual("pg_configurator/v2", expected["schema_version"])
        self.assertIn("artifact_hash", expected)
        self.assertEqual("pg-configurator-js", actual["generator"]["name"])
        self.assertEqual("pg_configurator/preview-v1", actual["schema_version"])
        self.assertEqual("PostgreSQLConfigurationPreview", actual["kind"])
        self.assertNotIn(
            "artifact_hash",
            actual,
            "the JavaScript build must not claim Python's canonical hash",
        )
        # A preview is the artifact minus its hash, not a different document.
        self.assertEqual(
            sorted(set(expected) - {"artifact_hash"}),
            sorted(actual),
            "the preview and the artifact carry different fields",
        )

    def test_rejected_command_lines_fail_the_same_way(self):
        for argv, label in REJECTED:
            with self.subTest(case=label):
                command = [*BASE, *argv]
                expected = python_cli(command)
                actual = node_cli(command, self.node)
                self.assertNotEqual(0, actual["exit"], f"{label}: the port accepted {command}")
                self.assertEqual(expected["exit"], actual["exit"], label)
                if expected.get("error"):
                    self.assertIn(expected["error"], actual["stderr"], label)

    def test_argparse_error_text_matches(self):
        """The message after `error:` is compared; the usage block is not."""

        cases = [
            (["--pg-version=7.4"], "argument --pg-version: invalid choice: '7.4'"),
            (["--db-disk-type=tape"], "argument --db-disk-type: invalid DiskType value: 'tape'"),
            (["--disk-score=abc"], "argument --disk-score: invalid float value: 'abc'"),
            (["--replica-count=x"], "argument --replica-count: invalid int value: 'x'"),
            (
                ["--pitr-enabled=maybe"],
                "argument --pitr-enabled: expected one of: true, false, 1, 0, yes, no, on, off",
            ),
            (["--nonexistent=1"], "unrecognized arguments: --nonexistent=1"),
            (
                ["--output-f=json"],
                "ambiguous option: --output-f=json could match --output-format, --output-file-name",
            ),
            (["--replica-count"], "argument --replica-count: expected one argument"),
            (["--out"], "argument --out/--output-file-name: expected one argument"),
            (
                ["--synchronous-standby-names", "--pg-version=17"],
                "argument --synchronous-standby-names: expected one argument",
            ),
            (
                ["--common-conf=false"],
                "argument --common-conf/--no-common-conf: ignored explicit argument 'false'",
            ),
            (["--debug=1"], "argument --debug: ignored explicit argument '1'"),
        ]
        for argv, fragment in cases:
            with self.subTest(case=argv[0]):
                actual = node_cli([*BASE, *argv], self.node)
                self.assertEqual(2, actual["exit"])
                self.assertIn(fragment, actual["stderr"])

    def test_input_json_expansion_matches(self):
        document = {
            "schema_version": "pg_configurator/input-v1",
            "inputs": {
                "db_cpu": "12",
                "db-ram": "48Gi",
                "pg_version": "17",
                "db_duty": "oltp",
                "pitr_enabled": False,
                "replica_count": 2,
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            command = [f"--input-json={path}"]
            expected = python_cli(command)
            actual = node_cli(command, self.node)
            self.assertEqual(0, actual["exit"], actual["stderr"])
            self.assertEqual(expected["stdout"], actual["stdout"])

            # An explicit option still wins over the document.
            command = [f"--input-json={path}", "--pg-version=15"]
            expected = python_cli(command)
            actual = node_cli(command, self.node)
            self.assertEqual(expected["stdout"], actual["stdout"])

    def test_out_writes_the_file_and_rotates_a_previous_one(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "pg.conf"
            command = [*BASE, "--pg-version=18", f"--out={target}"]
            first = node_cli(command, self.node)
            self.assertEqual(0, first["exit"], first["stderr"])
            self.assertTrue(target.exists())
            body = target.read_text(encoding="utf-8")

            second = node_cli(command, self.node)
            self.assertEqual(0, second["exit"], second["stderr"])
            backups = [item for item in Path(directory).iterdir() if item.name.endswith(".bak")]
            self.assertEqual(1, len(backups), "the previous output must be rotated")
            self.assertEqual(body, backups[0].read_text(encoding="utf-8"))

    def test_version_output_matches(self):
        expected = python_cli(["--version"])
        actual = node_cli(["--version"], self.node)
        self.assertEqual(expected["stdout"], actual["stdout"])

    def test_orchestration_options_are_refused_rather_than_ignored(self):
        for option in (
            "--machine",
            "--capabilities",
            "--validate-input",
            "--settings-history=9.6,15",
        ):
            with self.subTest(option=option):
                actual = node_cli([*BASE, "--pg-version=18", option], self.node)
                self.assertEqual(4, actual["exit"], f"{option} must be refused, not ignored")
                self.assertIn("not implemented in the JavaScript build", actual["stderr"])

    def test_host_defaults_agree_between_the_runtimes(self):
        """The two runtimes must read the same CPU count and RAM size."""

        node_defaults = json.loads(
            subprocess.run(
                [
                    self.node,
                    "--input-type=module",
                    "-e",
                    "import {cpus,totalmem} from 'node:os';"
                    f"const {{sizeTo,SYS_IEC}}=await import('{ROOT}/web/src/units.js');"
                    "process.stdout.write(JSON.stringify("
                    "{cpu:String(cpus().length),ram:sizeTo(totalmem(),SYS_IEC)}))",
                ],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
        )
        self.assertEqual(str(psutil.cpu_count()), node_defaults["cpu"])
        self.assertEqual(
            UnitConverter.size_to(psutil.virtual_memory().total, system=UnitConverter.sys_iec),
            node_defaults["ram"],
        )

    def test_host_name_agrees_between_the_runtimes(self):
        node_host = subprocess.run(
            [self.node, "-e", "process.stdout.write(require('node:os').hostname())"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        self.assertEqual(socket.gethostname(), node_host)


if __name__ == "__main__":
    unittest.main()
