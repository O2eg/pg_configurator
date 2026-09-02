"""Generate and freshness-check the numeric fixtures the JavaScript tests use.

The expression parity suite cannot reach everything. `round` is permitted in a
rule but no rule calls it, so a half-even/half-away defect in the port passes
that suite unnoticed; the same is true of every `UnitConverter` entry point and
of Python's float formatting. Those behaviours are pinned here instead: Python
computes the expected results, they are committed as data, and the Node test
suite asserts against them without needing Python at run time.

Both directions are checked. This test regenerates the fixtures and fails if
the committed file drifted, exactly like the generated data layer.
"""

import json
import math
import unittest
from pathlib import Path

from pg_configurator.configurator import UnitConverter

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "web" / "test" / "fixtures" / "python-numerics.json"

# Exact ties, the values the real call sites produce, and the boundaries where
# Python's repr switches to exponent notation.
ROUND_CASES = (
    # (value, digits) — ties first: these are where toFixed and Math.round
    # disagree with Python, and 0.0625 is reachable through --db-cpu.
    (0.0625, 3),
    (-0.0625, 3),
    (0.125, 2),
    (-0.125, 2),
    (0.375, 2),
    (2.5, 0),
    (-2.5, 0),
    (1.5, 0),
    (0.5, 0),
    (-0.5, 0),
    (1.25, 1),
    (-1.25, 1),
    (1.35, 1),
    # random_page_cost: round(4.0 - (disk_score / 100) * 2.9, 2)
    *((4.0 - (score / 100) * 2.9, 2) for score in (0, 15, 30, 45, 50, 75, 90, 92.5, 100)),
    # the three score fields: round(x, 4)
    *((value, 4) for value in (0.0, 33.333333333333336, 66.66666666666667, 100.0, 1.00005)),
    # size_cpu_to_ncores: round(value, 3)
    *((value, 3) for value in (8.0, 0.5, 0.0005, 1.0005, 2.0625, 0.001)),
    (2.675, 2),
    (-4.567, 2),
    (1e16 + 0.5, 0),
)

REPR_CASES = (
    0.0,
    -0.0,
    4.0,
    1.0,
    0.005,
    0.02,
    0.0075,
    0.1,
    1 / 3,
    2.5,
    -2.5,
    1e15,
    1e16,
    1e17,
    9999999999999998.0,
    1e-4,
    1e-5,
    1e-7,
    1.5e-5,
    123456789012345.6,
    43704647.68000001,
    1e21,
    2.2250738585072014e-308,
)

SIZE_SYSTEMS = {
    "sys_std": UnitConverter.sys_std,
    "sys_iec": UnitConverter.sys_iec,
    "sys_iso": UnitConverter.sys_iso,
    "sys_pg": UnitConverter.sys_pg,
}

SIZE_TO_VALUES = (
    0,
    1,
    1023,
    8192,
    43704647.68000001,
    1024**2,
    10 * 1024**2,
    1024**3,
    32 * 1024**3,
    1024**4,
    5 * 1024**4,
)

SIZE_FROM_VALUES = (
    "0",
    "512",
    "1B",
    "8kB",
    "1MB",
    "256Mi",
    "32Gi",
    "1Ti",
    "1.5Gi",
    " 64Mi ",
    "100",
    "2G",
    "1T",
)

SIZE_FROM_ERRORS = ("", "abc", "12Zi", "1..2Gi", "Gi")

CPU_VALUES = ("8", "1", "0.5", "500m", "250m", "0.0625", "1.0005", "16", "0.001", "1m")

CPU_ERRORS = ("", "abc", "8x", "1.2.3")


def _round(value, digits):
    return {"value": value, "digits": digits, "expected": round(value, digits)}


def _size_to(value, system_name):
    return {
        "value": value,
        "system": system_name,
        "expected": UnitConverter.size_to(value, system=SIZE_SYSTEMS[system_name]),
    }


def _size_from(text, system_name):
    try:
        return {
            "value": text,
            "system": system_name,
            "expected": UnitConverter.size_from(text, system=SIZE_SYSTEMS[system_name]),
        }
    except ValueError as error:
        return {"value": text, "system": system_name, "error": str(error)}


def build_fixtures():
    arithmetic = []
    for left, right in ((3, 4), (3, 4.0), (3.0, 4), (2.5, 2.5), (10, 4), (7, 7), (0, 5)):
        for operation, function in (
            ("add", lambda a, b: a + b),
            ("sub", lambda a, b: a - b),
            ("mul", lambda a, b: a * b),
            ("div", lambda a, b: a / b),
        ):
            result = function(left, right)
            arithmetic.append(
                {
                    "op": operation,
                    "left": {"k": "int" if isinstance(left, int) else "float", "v": left},
                    "right": {"k": "int" if isinstance(right, int) else "float", "v": right},
                    "expected": {
                        "k": "int" if isinstance(result, int) else "float",
                        "v": result,
                    },
                }
            )

    extremes = []
    for values in ((0.0, 0), (0, 0.0), (1, 1.0), (1.0, 1), (3, 4.5, 2), (5, 5.0, 5)):
        extremes.append(
            {
                "values": [
                    {"k": "int" if isinstance(item, int) else "float", "v": item} for item in values
                ],
                "max": {
                    "k": "int" if isinstance(max(values), int) else "float",
                    "v": max(values),
                },
                "min": {
                    "k": "int" if isinstance(min(values), int) else "float",
                    "v": min(values),
                },
            }
        )

    return {
        "round": [_round(value, digits) for value, digits in ROUND_CASES],
        "repr": [{"value": value, "expected": repr(value)} for value in REPR_CASES],
        "str_scalars": [
            {"value": {"k": "int", "v": 4}, "expected": "4"},
            {"value": {"k": "float", "v": 4.0}, "expected": "4.0"},
            {"value": {"k": "bool", "v": True}, "expected": "True"},
            {"value": {"k": "bool", "v": False}, "expected": "False"},
            {"value": {"k": "none"}, "expected": "None"},
            {"value": {"k": "str", "v": "on"}, "expected": "on"},
        ],
        "size_to": [_size_to(value, system) for system in SIZE_SYSTEMS for value in SIZE_TO_VALUES],
        "size_from": [
            _size_from(text, system) for system in SIZE_SYSTEMS for text in SIZE_FROM_VALUES
        ]
        + [_size_from(text, "sys_iec") for text in SIZE_FROM_ERRORS],
        "cpu_to_ncores": [
            {"value": value, "expected": UnitConverter.size_cpu_to_ncores(value)}
            for value in CPU_VALUES
        ]
        + [{"value": value, "error": _cpu_error(value)} for value in CPU_ERRORS],
        "arithmetic": arithmetic,
        "extremes": extremes,
        "ceil_floor": [
            {"value": value, "ceil": math.ceil(value), "floor": math.floor(value)}
            for value in (0.0, 1.0, 1.5, -1.5, 2.000001, 7.0, 8 / 3)
        ],
    }


def _cpu_error(value):
    try:
        UnitConverter.size_cpu_to_ncores(value)
    except ValueError as error:
        return str(error)
    raise AssertionError(f"{value!r} was expected to be rejected")


def render(fixtures):
    return json.dumps(fixtures, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


class TestNumericFixtures(unittest.TestCase):
    def test_committed_fixtures_are_current(self):
        expected = render(build_fixtures())
        self.assertTrue(
            FIXTURE.exists(),
            f"missing {FIXTURE}; regenerate it with: python {Path(__file__).relative_to(ROOT)}",
        )
        self.assertEqual(
            expected,
            FIXTURE.read_text(encoding="utf-8"),
            "web/test/fixtures/python-numerics.json is stale; regenerate it with: "
            f"python {Path(__file__).relative_to(ROOT)}",
        )

    def test_fixture_coverage_is_not_vacuous(self):
        fixtures = build_fixtures()
        self.assertGreaterEqual(len(fixtures["round"]), 30)
        self.assertGreaterEqual(len(fixtures["size_to"]), 40)
        self.assertTrue(any("error" in item for item in fixtures["size_from"]))
        self.assertTrue(any("error" in item for item in fixtures["cpu_to_ncores"]))

    def test_tie_cases_actually_exercise_half_even(self):
        # A tie fixture is only useful if half-even and half-away disagree on it.
        disagreements = 0
        for case in build_fixtures()["round"]:
            value, digits = case["value"], case["digits"]
            scaled = value * 10**digits
            if (
                abs(scaled - math.floor(scaled) - 0.5) < 1e-12
                and float(scaled).is_integer() is False
            ):
                away = math.floor(scaled) + (1 if value >= 0 else 0)
                if away != round(scaled):
                    disagreements += 1
        self.assertGreater(disagreements, 0, "no fixture distinguishes half-even from half-away")


if __name__ == "__main__":
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(render(build_fixtures()), encoding="utf-8")
    print(f"wrote {FIXTURE}")
