import unittest
from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace

from pg_configurator.configurator import PGConfigurator, UnitConverter


class TestConfiguratorCore(unittest.TestCase):
    def setUp(self):
        self.args = SimpleNamespace(output_file_name="", debug_mode=False)
        self.configurator = PGConfigurator(self.args, {})

    def make_conf(self, cpu="8", ram="16Gi", **kwargs):
        with redirect_stdout(StringIO()):
            return self.configurator.make_conf(cpu, ram, **kwargs)

    def test_profile_rules_do_not_leak_between_runs(self):
        base_before = self.make_conf(pg_version="18")
        profile_conf = self.make_conf(pg_version="18", conf_profiles="profile_1c")
        base_after = self.make_conf(pg_version="18")

        self.assertEqual(base_before, base_after)
        self.assertNotEqual(base_before["max_connections"], profile_conf["max_connections"])

    def test_scaling_is_clamped_to_declared_maximums(self):
        config = self.make_conf(cpu="192", ram="2Ti", pg_version="18")

        self.assertEqual("500", config["max_connections"])
        self.assertEqual("20", config["autovacuum_max_workers"])

    def test_reserved_memory_cannot_make_available_ram_non_positive(self):
        with self.assertRaisesRegex(ValueError, "Available RAM must be greater than 0"):
            self.make_conf(cpu="1", ram="128Mi", pg_version="18")

    def test_backend_common_profile_can_be_calculated(self):
        config = self.make_conf(pg_version="18", conf_profiles="profile_backend_common")

        self.assertIn("online_analyze.scale_factor", config)

    def test_removed_setting_is_not_generated_for_pg17_and_pg18(self):
        for version in ("17", "18"):
            for profile in (None, "profile_backend_perf"):
                with self.subTest(version=version, profile=profile):
                    config = self.make_conf(pg_version=version, conf_profiles=profile)
                    self.assertNotIn("old_snapshot_threshold", config)

    def test_all_bundled_profiles_match_version_setting_snapshots(self):
        profiles = (None,) + tuple(PGConfigurator.conf_profiles)

        for version in PGConfigurator.known_versions:
            for profile in profiles:
                for common_conf in (False, True):
                    with self.subTest(version=version, profile=profile, common_conf=common_conf):
                        self.make_conf(
                            pg_version=version, conf_profiles=profile, common_conf=common_conf
                        )

    def test_backend_buffer_parameters_follow_compatibility_matrix(self):
        for version in ("14", "15", "16"):
            with self.subTest(version=version):
                config = self.make_conf(pg_version=version, conf_profiles="profile_backend_perf")
                self.assertNotIn("subtrans_buffers", config)
                self.assertNotIn("xact_buffers", config)
                self.assertNotIn("subtransaction_buffers", config)
                self.assertNotIn("transaction_buffers", config)

        for version in ("17", "18"):
            with self.subTest(version=version):
                config = self.make_conf(pg_version=version, conf_profiles="profile_backend_perf")
                self.assertIn("subtransaction_buffers", config)
                self.assertIn("transaction_buffers", config)

    def test_unknown_core_setting_is_rejected(self):
        configurator = PGConfigurator(
            self.args, [{"name": "definitely_unknown_setting", "const": "on"}]
        )

        with self.assertRaisesRegex(ValueError, "not supported by PostgreSQL 18"):
            with redirect_stdout(StringIO()):
                configurator.make_conf("8", "16Gi", pg_version="18")

    def test_memory_parts_require_exact_positive_partition(self):
        invalid_arguments = (
            {"shared_buffers_part": 0.70, "client_mem_part": 0.20, "maintenance_mem_part": 0.06},
            {"shared_buffers_part": -0.10, "client_mem_part": 0.80, "maintenance_mem_part": 0.30},
            {"autovacuum_workers_mem_part": 0.60, "maintenance_conns_mem_part": 0.30},
        )

        for arguments in invalid_arguments:
            with self.subTest(arguments=arguments):
                with self.assertRaises(ValueError):
                    self.make_conf(pg_version="18", **arguments)

    def test_invalid_resource_and_range_inputs_are_rejected(self):
        invalid_calls = (
            (("0", "16Gi"), {}),
            (("invalid", "16Gi"), {}),
            (("8", "0Gi"), {}),
            (("8", "16Gi"), {"reserved_ram_percent": 100}),
            (("8", "16Gi"), {"reserved_system_ram": "-1Gi"}),
            (("8", "16Gi"), {"min_conns": 100, "max_conns": 50}),
            (("8", "16Gi"), {"min_conns": 1001, "max_conns": 1200, "conf_profiles": "profile_1c"}),
            (("8", "16Gi"), {"min_autovac_workers": 10, "max_autovac_workers": 4}),
            (("8", "16Gi"), {"min_maint_conns": 10, "max_maint_conns": 4}),
            (("8", "16Gi"), {"pg_version": "19"}),
            (("8", "16Gi"), {"replication_enabled": "false"}),
        )

        for positional, keyword in invalid_calls:
            with self.subTest(positional=positional, keyword=keyword):
                with self.assertRaises(ValueError):
                    self.make_conf(*positional, **keyword)

    def test_effective_cache_size_does_not_subtract_reserved_ram_twice(self):
        config = self.make_conf(pg_version="18")
        available_ram = UnitConverter.size_from(
            "16Gi", system=UnitConverter.sys_iec
        ) * 0.9 - UnitConverter.size_from("256Mi", system=UnitConverter.sys_iec)
        expected = UnitConverter.size_to(available_ram * 0.3, system=UnitConverter.sys_pg)

        self.assertEqual(expected, config["effective_cache_size"])

    def test_client_memory_settings_stay_within_budget(self):
        config = self.make_conf(cpu="8", ram="2Gi", pg_version="18", conf_profiles="profile_1c")
        available_ram = UnitConverter.size_from(
            "2Gi", system=UnitConverter.sys_iec
        ) * 0.9 - UnitConverter.size_from("256Mi", system=UnitConverter.sys_iec)
        client_budget = available_ram * 0.2
        per_connection = UnitConverter.size_from(
            config["work_mem"], system=UnitConverter.sys_pg
        ) + UnitConverter.size_from(config["temp_buffers"], system=UnitConverter.sys_pg)

        self.assertLess(int(config["max_connections"]), 1000)
        self.assertLessEqual(per_connection * int(config["max_connections"]), client_budget)

    def test_fractional_size_values_are_parsed_correctly(self):
        self.assertEqual(
            int(1.5 * 1024**3), UnitConverter.size_from("1.5Gi", system=UnitConverter.sys_iec)
        )


if __name__ == "__main__":
    unittest.main()
