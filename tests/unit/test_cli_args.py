import unittest
from contextlib import redirect_stderr
from io import StringIO

from pg_configurator.configurator import PGConfigurator


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


if __name__ == "__main__":
    unittest.main()
