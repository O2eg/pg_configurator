import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO

from pg_configurator.configurator import DutyDB, PGConfigurator, ReplicationMode, run_pgc


class TestCLIArguments(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
