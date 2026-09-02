"""Differential parity for the whole calculation, across every PostgreSQL major.

Python runs `make_conf`, the port runs its own, and the two results are
compared. The comparison is layered so a failure says what kind of divergence
it is:

L1  the user-visible answer — every generated setting value, the advisories, the
    overrides, and each parameter's source, rule, context and apply mode.
L2  the numbers behind it — every `raw_value` with its Python type, the
    calculation budgets and the normalized inputs.

Validation errors are compared too: a rejected input must be rejected by both
with the same message.
"""

import inspect
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from enum import Enum
from pathlib import Path
from types import SimpleNamespace

import pytest

from pg_configurator.configurator import PGConfigurator

ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "web" / "tools" / "parity-runner.mjs"

ALL_VERSIONS = ("9.6", "10", "11", "12", "13", "14", "15", "16", "17", "18")
DUTIES = ("statistic", "mixed", "oltp", "financial")
DISK_TYPES = ("SATA", "SAS", "SSD", "NVME", "NETWORK")
PROFILE_SETS = (
    "",
    "ext_perf",
    "profile_backend_common",
    "profile_backend_perf",
    "profile_backend_common,profile_backend_perf",
    "profile_backend_perf,profile_backend_common",
    "ext_perf,profile_backend_perf",
    "profile_1c",
)

BASE = {"cpu_cores": "8", "ram_value": "32Gi"}


def _case(label, **overrides):
    payload = dict(BASE)
    payload.update(overrides)
    return {"label": label, **payload}


def valid_cases():
    """A matrix that covers every major, duty, disk type and profile ordering."""

    cases = []
    # every major on the defaults
    for version in ALL_VERSIONS:
        cases.append(_case(f"major-{version}", pg_version=version))
    # every duty on the newest and the oldest major
    for version in ("9.6", "18"):
        for duty in DUTIES:
            cases.append(_case(f"duty-{duty}-{version}", pg_version=version, duty_db=duty))
    # every disk type, and an explicit score
    for disk in DISK_TYPES:
        cases.append(_case(f"disk-{disk}", pg_version="18", disk_type=disk))
    for score in (0, 24.9, 25, 49.5, 50, 74.9, 75, 89.9, 90, 100):
        cases.append(_case(f"disk-score-{score}", pg_version="18", disk_score=score))
    # profiles, including both orderings of an overlapping pair
    for profiles in PROFILE_SETS:
        for version in ("14", "18"):
            cases.append(
                _case(
                    f"profiles-{profiles or 'none'}-{version}",
                    pg_version=version,
                    conf_profiles=profiles,
                )
            )
    # replication shapes
    cases += [
        _case("replication-none", pg_version="18", replication_mode="none", pitr_enabled=False),
        _case("replication-none-pitr", pg_version="18", replication_mode="none"),
        _case("replication-physical-3", pg_version="18", replica_count=3),
        _case(
            "replication-logical",
            pg_version="18",
            replication_mode="logical",
            logical_subscription_count=4,
        ),
        _case(
            "replication-sync",
            pg_version="17",
            duty_db="financial",
            synchronous_standby_names="ANY 1 (r1, r2)",
        ),
        _case("replication-logical-12", pg_version="12", replication_mode="logical"),
    ]
    # platform, sizes and boundaries
    cases += [
        _case("platform-windows", pg_version="18", platform="WINDOWS"),
        _case("platform-windows-96", pg_version="9.6", platform="WINDOWS"),
        _case("tiny", pg_version="18", cpu_cores="1", ram_value="1Gi", min_conns=5),
        _case("fractional-cpu", pg_version="18", cpu_cores="500m", ram_value="2Gi", min_conns=5),
        _case("millicores", pg_version="18", cpu_cores="2500m", ram_value="8Gi"),
        _case("huge", pg_version="18", cpu_cores="192", ram_value="1Ti"),
        _case("db-size-small", pg_version="18", db_size="50Gi"),
        _case("db-size-medium", pg_version="18", db_size="500Gi"),
        _case("db-size-large", pg_version="18", db_size="4Ti"),
        _case("wal-tight", pg_version="18", wal_disk_budget="2Gi", peak_wal_rate="1Mi"),
        _case("wal-segment-64", pg_version="18", wal_segment_size="64Mi"),
        _case("wal-outage", pg_version="18", replica_outage_tolerance=7200),
        _case(
            "memory-parts",
            pg_version="18",
            shared_buffers_part=0.3,
            client_mem_part=0.25,
            maintenance_mem_part=0.15,
        ),
        _case(
            "memory-parts-shared-heavy",
            pg_version="18",
            shared_buffers_part=0.8,
            client_mem_part=0.03,
            maintenance_mem_part=0.02,
        ),
        _case(
            "memory-parts-at-the-total-limit",
            pg_version="18",
            shared_buffers_part=0.5,
            client_mem_part=0.3,
            maintenance_mem_part=0.05,
        ),
        _case("reserved-ram", pg_version="18", reserved_ram_percent=25, reserved_system_ram="1Gi"),
        _case("concurrency-factor", pg_version="18", work_mem_concurrency_factor=8.0),
        _case("conn-bounds", pg_version="18", min_conns=50, max_conns=120),
        _case("autovac-bounds", pg_version="18", min_autovac_workers=6, max_autovac_workers=12),
        _case("maint-bounds", pg_version="18", min_maint_conns=8, max_maint_conns=32),
        _case(
            "extensions-declared",
            pg_version="18",
            available_extensions="auto_explain,pg_stat_statements",
        ),
        _case(
            "extensions-declared-1c",
            pg_version="18",
            conf_profiles="profile_1c",
            available_extensions=(
                "auto_explain,pg_stat_statements,online_analyze,pg_store_plans,plantuner"
            ),
        ),
    ]
    return cases


def invalid_cases():
    """Inputs both implementations must reject, with the same message."""

    return [
        _case("bad-version", pg_version="7.4"),
        _case("bad-duty", pg_version="18", duty_db="archive"),
        _case("bad-disk", pg_version="18", disk_type="tape"),
        _case("bad-platform", pg_version="18", platform="PLAN9"),
        _case("disk-score-range", pg_version="18", disk_score=101),
        _case("negative-cpu", pg_version="18", cpu_cores="-2"),
        _case("zero-cpu", pg_version="18", cpu_cores="0"),
        _case("bad-cpu-text", pg_version="18", cpu_cores="eight"),
        _case("bad-ram", pg_version="18", ram_value="32Zi"),
        _case("reserved-too-large", pg_version="18", reserved_ram_percent=100),
        _case("memory-parts-overflow", pg_version="18", shared_buffers_part=0.8),
        _case("memory-parts-over-cap", pg_version="18", shared_buffers_part=0.81),
        _case(
            "memory-parts-just-over-total",
            pg_version="18",
            shared_buffers_part=0.8,
            client_mem_part=0.04,
            maintenance_mem_part=0.02,
        ),
        _case(
            "maintenance-parts",
            pg_version="18",
            autovacuum_workers_mem_part=0.7,
            maintenance_conns_mem_part=0.7,
        ),
        _case("conn-range", pg_version="18", min_conns=100, max_conns=50),
        _case("conn-zero", pg_version="18", min_conns=0),
        _case("autovac-range", pg_version="18", min_autovac_workers=10, max_autovac_workers=4),
        _case("unknown-profile", pg_version="18", conf_profiles="profile_zzz"),
        _case("repeated-profile", pg_version="18", conf_profiles="ext_perf,ext_perf"),
        _case("empty-profile", pg_version="18", conf_profiles="ext_perf,"),
        _case("exclusive-profile", pg_version="18", conf_profiles="profile_1c,ext_perf"),
        _case("1c-autovac", pg_version="18", conf_profiles="profile_1c", max_autovac_workers=3),
        _case("logical-on-96", pg_version="9.6", logical_subscription_count=2),
        _case(
            "logical-without-mode",
            pg_version="18",
            replication_mode="physical",
            logical_subscription_count=2,
        ),
        _case(
            "sync-without-replication",
            pg_version="18",
            replication_mode="none",
            synchronous_standby_names="ANY 1 (r1)",
        ),
        _case(
            "replication-conflict",
            pg_version="18",
            replication_enabled=True,
            replication_mode="none",
        ),
        _case("common-conf-off", pg_version="18", common_conf=False),
        _case("wal-budget-small", pg_version="18", wal_disk_budget="512Mi"),
        _case("wal-segment-odd", pg_version="18", wal_segment_size="24Mi"),
        _case("wal-segment-huge", pg_version="18", wal_segment_size="2Gi"),
        _case(
            "wal-budget-vs-segment",
            pg_version="18",
            wal_disk_budget="1Gi",
            wal_segment_size="256Mi",
        ),
        _case("zero-peak-wal", pg_version="18", peak_wal_rate="0"),
        _case("db-size-zero", pg_version="18", db_size="0"),
        _case("negative-outage", pg_version="18", replica_outage_tolerance=-1),
        _case("negative-replicas", pg_version="18", replica_count=-1),
        _case("concurrency-below-one", pg_version="18", work_mem_concurrency_factor=0.5),
        _case("ram-too-small", pg_version="18", cpu_cores="8", ram_value="256Mi"),
        _case(
            "extensions-missing",
            pg_version="18",
            available_extensions="pg_stat_statements",
        ),
    ]


def tag(value):
    if value is None:
        return {"k": "none"}
    if isinstance(value, bool):
        return {"k": "bool", "v": value}
    if isinstance(value, Enum):
        return {"k": "enum", "enum": type(value).__name__, "name": value.name}
    if isinstance(value, int):
        return {"k": "int", "v": value}
    if isinstance(value, float):
        return {"k": "float", "v": value}
    if isinstance(value, str):
        return {"k": "str", "v": value}
    if isinstance(value, (list, tuple)):
        return {"k": "tuple", "items": [tag(item) for item in value]}
    raise AssertionError(f"cannot tag {value!r}")


def python_result(case):
    arguments = {key: value for key, value in case.items() if key != "label"}
    cpu = arguments.pop("cpu_cores")
    ram = arguments.pop("ram_value")
    configurator = PGConfigurator(SimpleNamespace(output_file_name="", debug=False), None)
    config = configurator.make_conf(cpu, ram, **arguments)
    return {
        "config": config,
        "inputs": configurator.last_inputs,
        "extensions": configurator.last_extensions,
        "calculation": configurator.last_calculation,
        "advisories": configurator.last_advisories,
        "overrides": configurator.last_overrides,
        "parameters": {
            name: {**detail, "raw_value": tag(detail["raw_value"])}
            for name, detail in configurator.last_parameter_details.items()
        },
    }


def js_batch(cases):
    return {
        "cases": [
            {
                "label": case["label"],
                "cpu_cores": case["cpu_cores"],
                "ram_value": case["ram_value"],
                "options": {
                    key: value
                    for key, value in case.items()
                    if key not in ("label", "cpu_cores", "ram_value")
                },
            }
            for case in cases
        ]
    }


@pytest.mark.web
class TestDefaultParity(unittest.TestCase):
    """The two ports must agree on what an unspecified option means.

    Every case below that omits an option is really a test of the defaults, so
    drift here fails hundreds of subtests at once and names none of them. This
    compares the two tables directly, so the report is the parameter itself.
    """

    def test_the_port_declares_the_same_defaults(self):
        node = shutil.which("node")
        if node is None:
            if os.environ.get("PGC_REQUIRE_NODE"):
                raise AssertionError("node is required but was not found on PATH")
            raise unittest.SkipTest("node is not installed; skipping JavaScript parity")

        completed = subprocess.run(
            [node, str(RUNNER), "--defaults"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr.strip())
        ported = json.loads(completed.stdout)

        expected = {}
        for name, parameter in inspect.signature(PGConfigurator.make_conf).parameters.items():
            if parameter.default is inspect.Parameter.empty:
                continue
            value = parameter.default
            expected[name] = value.value if isinstance(value, Enum) else value

        self.maxDiff = None
        self.assertEqual(sorted(expected), sorted(ported), "the two default tables differ in shape")
        for name in sorted(expected):
            with self.subTest(parameter=name):
                self.assertEqual(expected[name], ported[name])


@pytest.mark.web
class TestConfigurationParity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        node = shutil.which("node")
        if node is None:
            if os.environ.get("PGC_REQUIRE_NODE"):
                raise AssertionError("node is required but was not found on PATH")
            raise unittest.SkipTest("node is not installed; skipping JavaScript parity")

        cls.cases = valid_cases() + invalid_cases()
        cls.expected = {}
        for case in cls.cases:
            try:
                cls.expected[case["label"]] = python_result(case)
            except (ValueError, TypeError) as error:
                cls.expected[case["label"]] = {"error": str(error)}

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "batch.json"
            path.write_text(json.dumps(js_batch(cls.cases)), encoding="utf-8")
            completed = subprocess.run(
                [node, str(RUNNER), "--configurations", str(path)],
                capture_output=True,
                text=True,
                check=False,
            )
        if completed.returncode != 0:
            raise AssertionError(f"parity-runner failed: {completed.stderr.strip()}")
        cls.actual = {item["label"]: item for item in json.loads(completed.stdout)["results"]}

    def _valid_pairs(self):
        for case in self.cases:
            label = case["label"]
            expected = self.expected[label]
            if "error" in expected:
                continue
            yield label, expected, self.actual[label]

    def test_generated_settings_match(self):
        for label, expected, actual in self._valid_pairs():
            with self.subTest(case=label):
                self.assertNotIn("error", actual, actual.get("error"))
                self.assertEqual(expected["config"], actual["config"])

    def test_advisories_match(self):
        for label, expected, actual in self._valid_pairs():
            with self.subTest(case=label):
                self.assertEqual(expected["advisories"], actual["advisories"])

    def test_overrides_match(self):
        for label, expected, actual in self._valid_pairs():
            with self.subTest(case=label):
                self.assertEqual(expected["overrides"], actual["overrides"])

    def test_parameter_explanations_match(self):
        keys = ("value", "source", "rule", "rule_kind", "context", "context_source", "apply_mode")
        for label, expected, actual in self._valid_pairs():
            with self.subTest(case=label):
                self.assertEqual(sorted(expected["parameters"]), sorted(actual["parameters"]))
                for name, detail in expected["parameters"].items():
                    self.assertEqual(
                        {key: detail[key] for key in keys},
                        {key: actual["parameters"][name][key] for key in keys},
                        f"{label}: {name}",
                    )

    def test_raw_values_match_with_their_python_type(self):
        for label, expected, actual in self._valid_pairs():
            with self.subTest(case=label):
                for name, detail in expected["parameters"].items():
                    self.assertEqual(
                        detail["raw_value"],
                        actual["parameters"][name]["raw_value"],
                        f"{label}: {name}",
                    )

    def test_calculation_matches(self):
        for label, expected, actual in self._valid_pairs():
            with self.subTest(case=label):
                self.assertEqual(expected["calculation"], actual["calculation"])

    def test_normalized_inputs_match(self):
        for label, expected, actual in self._valid_pairs():
            with self.subTest(case=label):
                self.assertEqual(expected["inputs"], actual["inputs"])

    def test_extensions_match(self):
        for label, expected, actual in self._valid_pairs():
            with self.subTest(case=label):
                self.assertEqual(expected["extensions"], actual["extensions"])

    def test_rejected_inputs_are_rejected_the_same_way(self):
        for case in self.cases:
            label = case["label"]
            expected = self.expected[label]
            if "error" not in expected:
                continue
            with self.subTest(case=label):
                actual = self.actual[label]
                self.assertIn("error", actual, f"{label}: the port accepted a rejected input")
                self.assertEqual(expected["error"], actual["error"]["message"])

    def test_the_matrix_covers_every_supported_major(self):
        covered = {
            self.expected[case["label"]]["inputs"]["pg_version"]
            for case in self.cases
            if "error" not in self.expected[case["label"]]
        }
        self.assertEqual(set(ALL_VERSIONS), covered)


if __name__ == "__main__":
    unittest.main()
