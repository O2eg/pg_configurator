import json
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
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

        first.advisories.append({"code": "first"})
        first.result_data = {"value": 1}

        self.assertEqual([], second.advisories)
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
        self.assertEqual("pg_configurator/v2", artifact["schema_version"])
        self.assertEqual(artifact, result.artifact)
        self.assertIsInstance(
            artifact["parameters"]["max_connections"]["raw_value"],
            int,
        )
        self.assertIn(
            artifact["parameters"]["shared_buffers"]["apply_mode"],
            ("reload", "restart"),
        )
        self.assertEqual("postmaster", artifact["parameters"]["shared_buffers"]["context"])
        self.assertEqual(
            "pg_settings_snapshot",
            artifact["parameters"]["shared_buffers"]["context_source"],
        )
        self.assertEqual("expression", artifact["parameters"]["shared_buffers"]["rule_kind"])
        self.assertEqual(
            ["pg_stat_statements", "auto_explain"],
            [extension["name"] for extension in artifact["extensions"]],
        )
        self.assertTrue(
            all(extension["availability"] == "unverified" for extension in artifact["extensions"])
        )

    def test_profile_advisories_and_caller_extension_inventory(self):
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

        codes = {item["code"]: item for item in artifact["advisories"]}
        self.assertEqual("warning", codes["profile_1c_ssl_disabled"]["severity"])
        self.assertEqual("ssl", codes["profile_1c_ssl_disabled"]["setting"])
        self.assertEqual("off", codes["profile_1c_ssl_disabled"]["actual"])
        self.assertEqual("assumption", codes["extension_inventory_not_verified"]["severity"])
        self.assertNotIn("preload_modules_not_declared", codes)
        self.assertTrue(any(item["to"] == "profile_1c" for item in artifact["overrides"]))
        self.assertTrue(
            all(
                extension["availability"] == "declared_available"
                for extension in artifact["extensions"]
            )
        )
        self.assertTrue(
            all(
                extension["availability_source"] == "caller_inventory"
                for extension in artifact["extensions"]
            )
        )

        with self.assertRaisesRegex(ValueError, "Required extensions are unavailable"):
            configurator.make_conf(
                "8",
                "16Gi",
                pg_version="18",
                conf_profiles="profile_1c",
                available_extensions="pg_stat_statements",
            )

    def test_existing_output_file_is_preserved_as_a_timestamped_backup(self):
        with TemporaryDirectory() as directory:
            target = Path(directory) / "candidate.json"
            target.write_text("original", encoding="utf-8")

            run_pgc(
                [
                    "--db-cpu=8",
                    "--db-ram=16Gi",
                    "--output-format=json",
                    f"--out={target}",
                ]
            )

            backups = list(target.parent.glob("candidate.json.*.bak"))
            self.assertEqual(1, len(backups))
            self.assertEqual("original", backups[0].read_text(encoding="utf-8"))
            self.assertEqual(
                "pg_configurator/v2",
                json.loads(target.read_text(encoding="utf-8"))["schema_version"],
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
        self.assertIn("autovacuum_vacuum_max_threshold", config)
        self.assertIn("idle_replication_slot_timeout", config)

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
