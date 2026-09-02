import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from pg_configurator.configurator import DutyDB, PGConfigurator, ReplicationMode, run_pgc


class TestCLIArguments(unittest.TestCase):
    def test_out_is_the_canonical_output_path_option(self):
        parser = PGConfigurator.get_arg_parser()

        self.assertEqual(
            "candidate.conf", parser.parse_args(["--out=candidate.conf"]).output_file_name
        )
        self.assertEqual(
            "legacy.conf",
            parser.parse_args(["--output-file-name=legacy.conf"]).output_file_name,
        )

    def test_replication_enabled_accepts_false_values(self):
        parser = PGConfigurator.get_arg_parser()

        for value in ("False", "false", "0", "no", "off"):
            with self.subTest(value=value):
                args = parser.parse_args([f"--replication-enabled={value}"])
                self.assertFalse(args.replication_enabled)

    def test_replication_enabled_accepts_true_values(self):
        parser = PGConfigurator.get_arg_parser()

        for value in ("True", "true", "1", "yes", "on"):
            with self.subTest(value=value):
                args = parser.parse_args([f"--replication-enabled={value}"])
                self.assertTrue(args.replication_enabled)

    def test_replication_enabled_rejects_unknown_value(self):
        parser = PGConfigurator.get_arg_parser()

        with redirect_stderr(StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(["--replication-enabled", "sometimes"])

    def test_replication_mode_and_wal_capacity_inputs(self):
        parser = PGConfigurator.get_arg_parser()
        args = parser.parse_args(
            [
                "--replication-mode=logical",
                "--logical-subscription-count=3",
                "--peak-wal-rate=32Mi",
                "--replica-outage-tolerance=1800",
                "--wal-disk-budget=128Gi",
            ]
        )

        self.assertEqual(ReplicationMode.LOGICAL, args.replication_mode)
        self.assertEqual(3, args.logical_subscription_count)
        self.assertEqual("32Mi", args.peak_wal_rate)
        self.assertEqual(1800, args.replica_outage_tolerance)
        self.assertEqual("128Gi", args.wal_disk_budget)

    def test_replication_alias_is_unset_by_default_and_explicit_values_must_agree(self):
        parser = PGConfigurator.get_arg_parser()

        self.assertIsNone(parser.parse_args([]).replication_enabled)
        with redirect_stdout(StringIO()):
            run_pgc(["--replication-mode=none"])
            run_pgc(
                [
                    "--replication-enabled=false",
                    "--replication-mode=none",
                ]
            )

        for arguments in (
            ["--replication-enabled=true", "--replication-mode=none"],
            ["--replication-enabled=false", "--replication-mode=physical"],
        ):
            with self.subTest(arguments=arguments):
                with self.assertRaisesRegex(ValueError, "conflicts"):
                    run_pgc(arguments)

    def test_oltp_duty_is_available_from_cli(self):
        args = PGConfigurator.get_arg_parser().parse_args(["--db-duty=oltp"])

        self.assertEqual(DutyDB.OLTP, args.db_duty)

    def test_database_size_is_available_for_statistics_tiering(self):
        args = PGConfigurator.get_arg_parser().parse_args(["--db-size=2Ti"])

        self.assertEqual("2Ti", args.db_size)

    def test_common_observability_is_on_by_default_and_disable_is_rejected(self):
        parser = PGConfigurator.get_arg_parser()

        defaults = parser.parse_args([])
        self.assertTrue(defaults.common_conf)
        self.assertEqual("18", defaults.pg_version)
        with self.assertRaisesRegex(ValueError, "common_conf cannot be disabled"):
            run_pgc(["--no-common-conf"])

    def test_debug_reports_normalized_inputs_and_evaluated_values(self):
        stderr = StringIO()
        with redirect_stderr(stderr), redirect_stdout(StringIO()):
            run_pgc(["--db-cpu=500m", "--db-ram=4Gi", "--debug"])

        debug_output = stderr.getvalue()
        self.assertIn('"cpu_cores": 0.5', debug_output)
        self.assertIn("# normalized_inputs = ", debug_output)
        self.assertIn("# rule shared_buffers:", debug_output)
        self.assertIn("value=", debug_output)

    def test_patroni_json_respects_minimum_replication_slots(self):
        stdout = StringIO()
        with redirect_stdout(stdout):
            run_pgc(
                [
                    "--db-cpu=16",
                    "--db-ram=64Gi",
                    "--pg-version=18",
                    "--output-format=patroni-json",
                ]
            )

        parameters = json.loads(stdout.getvalue())["postgresql"]["parameters"]
        self.assertEqual("4", parameters["max_replication_slots"])

    def test_machine_capabilities_use_versioned_envelope(self):
        stdout = StringIO()
        with redirect_stdout(stdout):
            run_pgc(["--component-capabilities", "--machine", "--request-id=test-capabilities"])

        payload = json.loads(stdout.getvalue())
        self.assertEqual("pg_play/component/v2", payload["contract_version"])
        self.assertEqual("pg_configurator", payload["component"])
        self.assertEqual("test-capabilities", payload["request_id"])
        self.assertEqual("succeeded", payload["status"])
        self.assertEqual("pg_play/capabilities/v1", payload["result"]["capability_schema_version"])
        self.assertEqual(
            "--component-capabilities",
            payload["result"]["machine_interface"]["capabilities_option"],
        )
        self.assertIn("generate", payload["result"]["commands"])

    def test_orchestration_plumbing_is_hidden_from_human_help(self):
        help_text = PGConfigurator.get_arg_parser().format_help()

        for option in (
            "--capabilities",
            "--component-capabilities",
            "--machine",
            "--request-id",
            "--input-json",
            "--validate-input",
        ):
            self.assertNotIn(option, help_text)

    def test_input_json_is_strict_and_explicit_cli_values_win(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "input.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": "pg_configurator/input-v1",
                        "inputs": {
                            "db_cpu": "4",
                            "db_ram": "8Gi",
                            "pg_version": "18",
                            "db_duty": "statistic",
                            "pitr_enabled": False,
                        },
                    }
                ),
                encoding="utf-8",
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                result = run_pgc(
                    [
                        f"--input-json={path}",
                        "--db-cpu=8",
                        "--validate-input",
                        "--machine",
                    ]
                )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(8.0, result.artifact["inputs"]["cpu_cores"])
        self.assertEqual("18", result.artifact["inputs"]["pg_version"])
        self.assertFalse(result.artifact["inputs"]["pitr_enabled"])
        self.assertTrue(payload["result"]["valid"])

    def test_machine_patroni_output_contains_exact_applicable_document(self):
        stdout = StringIO()
        with redirect_stdout(stdout):
            run_pgc(
                [
                    "--db-cpu=16",
                    "--db-ram=64Gi",
                    "--pg-version=18",
                    "--output-format=patroni-json",
                    "--machine",
                ]
            )

        payload = json.loads(stdout.getvalue())
        parameters = payload["result"]["document"]["postgresql"]["parameters"]
        self.assertEqual("4", parameters["max_replication_slots"])
        self.assertEqual(
            payload["artifacts"][0]["hash"],
            payload["result"]["artifact"]["artifact_hash"],
        )


if __name__ == "__main__":
    unittest.main()
