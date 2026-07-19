import json
import unittest
from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace

from pg_configurator import PGConfigurator, PGConfiguratorResult
from pg_configurator.common import ResultCode
from pg_configurator.configurator import UnitConverter, run_pgc


class TestPublicAPI(unittest.TestCase):
    def setUp(self):
        self.args = SimpleNamespace(output_file_name="", debug_mode=False)

    def test_result_instances_do_not_share_state(self):
        first = PGConfiguratorResult()
        second = PGConfiguratorResult()

        first.warnings.append("first")
        first.result_data = {"value": 1}

        self.assertEqual([], second.warnings)
        self.assertIsNone(second.result_data)

    def test_version_returns_result_contract(self):
        stdout = StringIO()
        with redirect_stdout(stdout):
            result = run_pgc(["--version"])

        self.assertEqual(ResultCode.DONE, result.result_code)
        self.assertIn("version", result.result_data)
        self.assertTrue(stdout.getvalue().startswith("pg-configurator "))

    def test_json_output_is_single_versioned_artifact(self):
        stdout = StringIO()
        with redirect_stdout(stdout):
            result = run_pgc(
                [
                    "--db-cpu=8",
                    "--db-ram=16Gi",
                    "--pg-version=18",
                    "--output-format=json",
                ]
            )

        artifact = json.loads(stdout.getvalue())
        self.assertEqual("pg_configurator/v1", artifact["schema_version"])
        self.assertEqual(artifact, result.artifact)
        self.assertIsInstance(
            artifact["parameters"]["max_connections"]["raw_value"],
            int,
        )
        self.assertIn(
            artifact["parameters"]["shared_buffers"]["apply_mode"],
            ("reload", "restart"),
        )

    def test_profile_warnings_and_extension_preflight(self):
        configurator = PGConfigurator(self.args, [])
        required_extensions = (
            "auto_explain,online_analyze,pg_stat_statements,pg_store_plans,plantuner"
        )
        config = configurator.make_conf(
            "8",
            "16Gi",
            pg_version="18",
            conf_profiles="profile_1c",
            available_extensions=required_extensions,
        )
        artifact = configurator.build_artifact(config)

        self.assertTrue(any("disables SSL" in warning for warning in artifact["warnings"]))
        self.assertTrue(any(item["to"] == "profile_1c" for item in artifact["overrides"]))

        with self.assertRaisesRegex(ValueError, "Required extensions are unavailable"):
            configurator.make_conf(
                "8",
                "16Gi",
                pg_version="18",
                conf_profiles="profile_1c",
                available_extensions="pg_stat_statements",
            )

    def test_work_mem_concurrency_factor_reduces_setting(self):
        configurator = PGConfigurator(self.args, [])
        factor_one = configurator.make_conf(
            "8", "16Gi", pg_version="18", work_mem_concurrency_factor=1
        )
        factor_four = configurator.make_conf(
            "8", "16Gi", pg_version="18", work_mem_concurrency_factor=4
        )

        self.assertGreater(
            UnitConverter.size_from(factor_one["work_mem"], system=UnitConverter.sys_pg),
            UnitConverter.size_from(factor_four["work_mem"], system=UnitConverter.sys_pg),
        )

    def test_pg18_has_explicit_async_io_and_autovacuum_rules(self):
        configurator = PGConfigurator(self.args, [])
        config = configurator.make_conf("8", "16Gi", pg_version="18")

        self.assertEqual("worker", config["io_method"])
        self.assertIn("io_workers", config)
        self.assertIn("io_max_concurrency", config)
        self.assertIn("autovacuum_worker_slots", config)

    def test_settings_history_rejects_invalid_requests(self):
        configurator = PGConfigurator(self.args, [])

        with self.assertRaises(ValueError):
            configurator.settings_history(["18"])
        with self.assertRaises(ValueError):
            configurator.settings_history(["18", "19"])

        result = configurator.settings_history(["17", "18"])
        self.assertEqual(ResultCode.DONE, result.result_code)
        self.assertEqual(
            "pg_configurator/settings-history-v1",
            result.artifact["schema_version"],
        )


if __name__ == "__main__":
    unittest.main()
