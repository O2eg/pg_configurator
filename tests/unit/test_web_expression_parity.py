"""Differential parity for the exported rule expressions.

Python is the oracle. It runs the real calculation, records the context it
handed the rule evaluator, the ``make_conf`` locals the ported closures need,
and the value every rule expression produced. The whole batch goes to one Node
process, and the results are compared value and Python type.

This is deliberately narrower than the full configuration comparison that comes
later: it isolates the evaluator, the numeric model and the eight callables, so
a divergence points at an expression rather than at one of 150 settings.

Node is optional for a plain ``pytest`` run and required in CI: set
``PGC_REQUIRE_NODE=1`` to turn a missing interpreter into a failure.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from enum import Enum
from pathlib import Path
from types import SimpleNamespace

import pytest

from pg_configurator import configurator as configurator_module
from pg_configurator.configurator import PGConfigurator

ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "web" / "tools" / "parity-runner.mjs"

# Values the ported closures capture from make_conf's scope. Everything else a
# rule can reach is in the rule context itself.
CLOSURE_ENVIRONMENT = (
    "cpu_threads",
    "connection_capacity",
    "connections_per_cpu",
    "max_conns",
    "total_ram_in_bytes",
    "client_mem_part",
    "default_temp_buffers_in_bytes",
    "min_work_mem_in_bytes",
    "work_mem_concurrency_factor",
    "hash_mem_multiplier",
    "mebibyte",
    "duty_work_mem_cap_bytes",
    "disk_scores",
    "system_scores",
)

# JavaScript numbers are doubles: an integer beyond 2**53-1 would not survive
# the JSON round trip, and silently rounding a byte count is not acceptable.
MAX_SAFE_INTEGER = 2**53 - 1

CASES = (
    ("baseline-18", {"pg_version": "18"}),
    ("baseline-9.6", {"pg_version": "9.6"}),
    ("oltp-small", {"pg_version": "17", "duty_db": "oltp", "cpu_cores": "2", "ram_value": "4Gi"}),
    (
        "financial-sync",
        {
            "pg_version": "17",
            "duty_db": "financial",
            "synchronous_standby_names": "ANY 1 (r1, r2)",
            "replication_mode": "physical",
        },
    ),
    (
        "statistic-large",
        {"pg_version": "16", "duty_db": "statistic", "cpu_cores": "64", "ram_value": "512Gi"},
    ),
    ("mixed-nvme", {"pg_version": "15", "disk_type": "NVME", "disk_score": 92.5}),
    ("profile-1c", {"pg_version": "14", "conf_profiles": "profile_1c"}),
    (
        "profile-backend",
        {"pg_version": "18", "conf_profiles": "profile_backend_common,profile_backend_perf"},
    ),
    ("profile-ext-perf", {"pg_version": "13", "conf_profiles": "ext_perf"}),
    (
        "logical-replication",
        {"pg_version": "18", "replication_mode": "logical", "logical_subscription_count": 3},
    ),
    ("no-replication", {"pg_version": "12", "replication_mode": "none", "pitr_enabled": False}),
    (
        "fractional-cpu",
        {"pg_version": "18", "cpu_cores": "500m", "ram_value": "2Gi", "min_conns": 5},
    ),
    ("windows-platform", {"pg_version": "11", "platform": "WINDOWS"}),
    ("tight-memory", {"pg_version": "10", "cpu_cores": "1", "ram_value": "1Gi", "min_conns": 5}),
)

DEFAULT_INPUTS = {"cpu_cores": "8", "ram_value": "32Gi"}


def tag(value):
    """Serialize a Python value together with the type Python gave it."""

    if value is None:
        return {"k": "none"}
    if isinstance(value, bool):
        return {"k": "bool", "v": value}
    if isinstance(value, Enum):
        return {"k": "enum", "enum": type(value).__name__, "name": value.name}
    if isinstance(value, int):
        if abs(value) > MAX_SAFE_INTEGER:
            raise AssertionError(f"Integer outside the supported numeric domain: {value}")
        return {"k": "int", "v": value}
    if isinstance(value, float):
        return {"k": "float", "v": value}
    if isinstance(value, str):
        return {"k": "str", "v": value}
    if isinstance(value, (list, tuple)):
        kind = "list" if isinstance(value, list) else "tuple"
        return {"k": kind, "items": [tag(item) for item in value]}
    # Functions and enum classes are provided by the port itself.
    return {"k": "opaque"}


class _Recorder:
    """Stand-in for RuleEvaluator that records its inputs and every result."""

    def __init__(self, real):
        self.real = real
        self.context = None
        self.environment = None
        self.expressions = []

    def __call__(self, context, *, allowed_callables, allowed_attribute_roots):
        # make_conf builds the evaluator from its own scope; at this point every
        # local the ported closures capture already exists.
        frame_locals = sys._getframe(1).f_locals
        self.context = dict(context)
        self.environment = {
            name: frame_locals[name] for name in CLOSURE_ENVIRONMENT if name in frame_locals
        }
        evaluator = self.real(
            context,
            allowed_callables=allowed_callables,
            allowed_attribute_roots=allowed_attribute_roots,
        )
        recorder = self

        class Recording:
            def evaluate(self, expression):
                value = evaluator.evaluate(expression)
                recorder.expressions.append((expression, value))
                return value

        return Recording()


def build_batch():
    """Run every case through Python and collect what the port must reproduce."""

    cases = []
    for label, overrides in CASES:
        recorder = _Recorder(configurator_module.RuleEvaluator)
        original = configurator_module.RuleEvaluator
        configurator_module.RuleEvaluator = recorder
        try:
            arguments = dict(DEFAULT_INPUTS)
            arguments.update(overrides)
            configurator = PGConfigurator(SimpleNamespace(output_file_name="", debug=False), None)
            configurator.make_conf(
                arguments.pop("cpu_cores"), arguments.pop("ram_value"), **arguments
            )
        finally:
            configurator_module.RuleEvaluator = original

        missing = set(CLOSURE_ENVIRONMENT) - set(recorder.environment)
        if missing:
            raise AssertionError(f"make_conf no longer defines: {', '.join(sorted(missing))}")

        cases.append(
            {
                "label": label,
                "environment": {name: tag(value) for name, value in recorder.environment.items()},
                "context": {name: tag(value) for name, value in recorder.context.items()},
                "expressions": [
                    {"expr": expression, "expected": tag(value)}
                    for expression, value in recorder.expressions
                ],
            }
        )
    return {"cases": cases}


@pytest.mark.web
class TestExpressionParity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        node = shutil.which("node")
        if node is None:
            if os.environ.get("PGC_REQUIRE_NODE"):
                raise AssertionError("node is required but was not found on PATH")
            raise unittest.SkipTest("node is not installed; skipping JavaScript parity")

        cls.batch = build_batch()
        with tempfile.TemporaryDirectory() as directory:
            batch_path = Path(directory) / "batch.json"
            batch_path.write_text(json.dumps(cls.batch), encoding="utf-8")
            completed = subprocess.run(
                [node, str(RUNNER), "--expressions", str(batch_path)],
                capture_output=True,
                text=True,
                check=False,
            )
        if completed.returncode != 0:
            raise AssertionError(f"parity-runner failed: {completed.stderr.strip()}")
        cls.actual = json.loads(completed.stdout)

    def _pairs(self):
        for expected_case, actual_case in zip(
            self.batch["cases"], self.actual["results"], strict=True
        ):
            self.assertEqual(expected_case["label"], actual_case["label"])
            for expected, actual in zip(
                expected_case["expressions"], actual_case["values"], strict=True
            ):
                yield expected_case["label"], expected, actual

    def test_every_expression_matches_value_and_type(self):
        for label, expected, actual in self._pairs():
            with self.subTest(case=label, expression=expected["expr"]):
                self.assertNotIn(
                    "error",
                    actual,
                    f"the port raised where Python returned {expected['expected']}",
                )
                self.assertEqual(expected["expected"], actual["actual"])

    def test_the_matrix_reaches_every_exported_expression(self):
        exported = json.loads((ROOT / "web" / "data" / "rules.json").read_text(encoding="utf-8"))[
            "payload"
        ]["expressions"]
        exercised = {item["expr"] for case in self.batch["cases"] for item in case["expressions"]}
        unreached = sorted(set(exported) - exercised)
        self.assertEqual(
            [],
            unreached,
            "these exported expressions are never evaluated by the matrix: " + ", ".join(unreached),
        )

    def test_the_port_supplies_every_callable_and_root(self):
        # A context binding Python provides as a function or enum class must
        # exist on the port; parity-runner refuses the batch otherwise, so
        # reaching this assertion at all means the surfaces line up.
        for expected_case, actual_case in zip(
            self.batch["cases"], self.actual["results"], strict=True
        ):
            with self.subTest(case=expected_case["label"]):
                opaque = {
                    name
                    for name, value in expected_case["context"].items()
                    if value["k"] == "opaque"
                }
                self.assertTrue(opaque.issubset(set(actual_case["contextNames"])))

    def test_values_stay_inside_the_supported_numeric_domain(self):
        for label, expected, _ in self._pairs():
            value = expected["expected"]
            if value["k"] == "int":
                with self.subTest(case=label, expression=expected["expr"]):
                    self.assertLessEqual(abs(value["v"]), MAX_SAFE_INTEGER)


if __name__ == "__main__":
    unittest.main()
