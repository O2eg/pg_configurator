import unittest
from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace

from pg_configurator.configurator import (
    DiskType,
    DutyDB,
    PGConfigurator,
    Platform,
    ReplicationMode,
    UnitConverter,
    quote_postgresql_conf_value,
    unquote_postgresql_conf_value,
)


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

        self.assertLessEqual(int(config["max_connections"]), 500)
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
                with self.subTest(version=version, profile=profile):
                    self.make_conf(pg_version=version, conf_profiles=profile)

    def test_mandatory_common_configuration_cannot_be_disabled(self):
        with self.assertRaisesRegex(ValueError, "common_conf cannot be disabled"):
            self.make_conf(pg_version="18", common_conf=False)

    def test_backend_profile_keeps_pg17_slru_buffers_on_safe_auto_defaults(self):
        for version in ("14", "15", "16", "17", "18"):
            with self.subTest(version=version):
                config = self.make_conf(pg_version=version, conf_profiles="profile_backend_perf")
                self.assertNotIn("subtrans_buffers", config)
                self.assertNotIn("xact_buffers", config)
                self.assertNotIn("subtransaction_buffers", config)
                self.assertNotIn("transaction_buffers", config)

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

    def test_memory_budget_bounds_are_per_part(self):
        """shared_buffers may claim more than the per-session budgets around it.

        It is allocated once at startup; client and maintenance memory are
        multiplied by the sessions using them at the same time, so they keep the
        smaller ceiling. The total stops below the envelope check's 90% because
        the envelope also counts lock tables and the per-backend reserve.
        """

        generous = self.make_conf(
            pg_version="18",
            shared_buffers_part=0.8,
            client_mem_part=0.03,
            maintenance_mem_part=0.02,
        )
        modest = self.make_conf(
            pg_version="18",
            shared_buffers_part=0.25,
            client_mem_part=0.03,
            maintenance_mem_part=0.02,
        )
        self.assertGreater(
            UnitConverter.size_from(generous["shared_buffers"], system=UnitConverter.sys_pg),
            2 * UnitConverter.size_from(modest["shared_buffers"], system=UnitConverter.sys_pg),
        )

        rejected = (
            ({"shared_buffers_part": 0.81}, "shared_buffers_part .* not greater than 0.8"),
            ({"client_mem_part": 0.41}, "client_mem_part .* not greater than 0.4"),
            ({"maintenance_mem_part": 0.41}, "maintenance_mem_part .* not greater than 0.4"),
            (
                {"shared_buffers_part": 0.8, "client_mem_part": 0.04},
                "at most 85% of available RAM; got 86.00%",
            ),
        )
        for arguments, message in rejected:
            with self.subTest(arguments=arguments):
                with self.assertRaisesRegex(ValueError, message):
                    self.make_conf(
                        pg_version="18",
                        **{
                            "client_mem_part": 0.02,
                            "maintenance_mem_part": 0.02,
                            **arguments,
                        },
                    )

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
        expected_bytes = (
            available_ram * 0.7
            - int(config["max_connections"]) * 10 * 1024**2
            - self.configurator.last_calculation["lock_memory_envelope_bytes"]
        )
        actual_bytes = UnitConverter.size_from(
            config["effective_cache_size"], system=UnitConverter.sys_pg
        )

        self.assertLessEqual(abs(actual_bytes - expected_bytes), 1024**2)
        self.assertGreaterEqual(
            actual_bytes,
            UnitConverter.size_from(config["shared_buffers"], system=UnitConverter.sys_pg),
        )

    def test_reserved_connection_slots_follow_profile_connection_target(self):
        config = self.make_conf(
            pg_version="18",
            conf_profiles="profile_1c",
        )
        max_connections = int(config["max_connections"])

        self.assertEqual(
            max(3, min(10, int(max_connections * 0.03))),
            int(config["superuser_reserved_connections"]),
        )
        self.assertEqual(
            max(1, min(5, int(max_connections * 0.02))),
            int(config["reserved_connections"]),
        )

    def test_client_memory_settings_stay_within_budget(self):
        config = self.make_conf(cpu="8", ram="2Gi", pg_version="18", conf_profiles="profile_1c")
        available_ram = UnitConverter.size_from(
            "2Gi", system=UnitConverter.sys_iec
        ) * 0.9 - UnitConverter.size_from("256Mi", system=UnitConverter.sys_iec)
        client_budget = available_ram * 0.2
        active_sessions = min(int(config["max_connections"]), 16)
        concurrent_query_memory = active_sessions * (
            UnitConverter.size_from(config["temp_buffers"], system=UnitConverter.sys_pg)
            + UnitConverter.size_from(config["work_mem"], system=UnitConverter.sys_pg)
            * 4
            * float(config["hash_mem_multiplier"])
        )

        self.assertLess(int(config["max_connections"]), 1000)
        self.assertLessEqual(concurrent_query_memory, client_budget * 1.01)

    def test_durability_invariants_hold_for_every_duty_and_replication_mode(self):
        for duty in DutyDB:
            for mode in ReplicationMode:
                with self.subTest(duty=duty, mode=mode):
                    config = self.make_conf(
                        pg_version="18",
                        duty_db=duty,
                        replication_mode=mode,
                    )
                    self.assertEqual("on", config["fsync"])
                    self.assertEqual("on", config["full_page_writes"])
                    self.assertEqual("on", config["synchronous_commit"])
                    if mode == ReplicationMode.LOGICAL:
                        self.assertEqual("logical", config["wal_level"])
                    elif mode == ReplicationMode.PHYSICAL:
                        self.assertEqual("replica", config["wal_level"])

    def test_financial_remote_apply_requires_named_synchronous_standby(self):
        local = self.make_conf(pg_version="18", duty_db=DutyDB.FINANCIAL)
        remote = self.make_conf(
            pg_version="18",
            duty_db=DutyDB.FINANCIAL,
            synchronous_standby_names="FIRST 1 (standby1)",
        )

        self.assertEqual("on", local["synchronous_commit"])
        self.assertEqual("remote_apply", remote["synchronous_commit"])
        self.assertEqual("'FIRST 1 (standby1)'", remote["synchronous_standby_names"])

        with self.assertRaisesRegex(ValueError, "requires physical or logical replication"):
            self.make_conf(
                pg_version="18",
                replication_enabled=False,
                pitr_enabled=False,
                synchronous_standby_names="FIRST 1 (standby1)",
            )

    def test_external_overrides_cannot_bypass_durability_or_observability(self):
        unsafe_rule_sets = (
            ([{"name": "synchronous_commit", "const": "off"}], "synchronous_commit"),
            ([{"name": "wal_level", "const": "minimal"}], "PITR or replication"),
            ([{"name": "logging_collector", "const": "off"}], "CSV logging"),
            (
                [{"name": "shared_preload_libraries", "const": "'auto_explain'"}],
                "auto_explain and pg_stat_statements",
            ),
        )

        for rules, message in unsafe_rule_sets:
            with self.subTest(rules=rules):
                configurator = PGConfigurator(self.args, rules)
                with self.assertRaisesRegex(ValueError, message):
                    with redirect_stdout(StringIO()):
                        configurator.make_conf("8", "16Gi", pg_version="18")

    def test_wal_retention_is_equivalent_across_pg12_to_pg13_transition(self):
        pg12 = self.make_conf(pg_version="12")
        pg13 = self.make_conf(pg_version="13")
        pg12_bytes = int(pg12["wal_keep_segments"]) * 16 * 1024**2
        pg13_bytes = UnitConverter.size_from(pg13["wal_keep_size"], UnitConverter.sys_pg)

        self.assertLessEqual(abs(pg12_bytes - pg13_bytes), 16 * 1024**2)
        self.assertIn("max_slot_wal_keep_size", pg13)

    def test_worker_pools_reserve_capacity_for_all_consumers(self):
        for version in PGConfigurator.known_versions:
            for mode in ReplicationMode:
                with self.subTest(version=version, mode=mode):
                    config = self.make_conf(
                        cpu="32",
                        ram="64Gi",
                        pg_version=version,
                        replication_mode=mode,
                        logical_subscription_count=(
                            3 if mode == ReplicationMode.LOGICAL and version != "9.6" else 0
                        ),
                    )
                    parallel = int(
                        config.get(
                            "max_parallel_workers",
                            config["max_parallel_workers_per_gather"],
                        )
                    )
                    logical = int(config.get("max_logical_replication_workers", 0))
                    self.assertGreaterEqual(
                        int(config["max_worker_processes"]),
                        parallel + logical + 4,
                    )

    def test_disk_score_drives_planner_and_io_settings_consistently(self):
        slow = self.make_conf(pg_version="18", disk_type=DiskType.NVME, disk_score=10)
        fast = self.make_conf(pg_version="18", disk_type=DiskType.SATA, disk_score=95)

        self.assertGreater(float(slow["random_page_cost"]), float(fast["random_page_cost"]))
        self.assertLess(
            int(slow["effective_io_concurrency"]),
            int(fast["effective_io_concurrency"]),
        )
        self.assertEqual("-1", slow["io_max_concurrency"])
        self.assertEqual("-1", fast["io_max_concurrency"])
        self.assertLess(
            int(slow["autovacuum_vacuum_cost_limit"]),
            int(fast["autovacuum_vacuum_cost_limit"]),
        )
        self.assertGreater(
            int(slow["autovacuum_vacuum_cost_delay"].removesuffix("ms")),
            int(fast["autovacuum_vacuum_cost_delay"].removesuffix("ms")),
        )

    def test_statistics_target_is_bounded_by_duty_database_size_and_resources(self):
        expected = {
            DutyDB.FINANCIAL: "1000",
            DutyDB.OLTP: "2500",
            DutyDB.MIXED: "5000",
            DutyDB.STATISTIC: "5000",
        }
        for duty, target in expected.items():
            with self.subTest(duty=duty):
                config = self.make_conf(
                    cpu="32",
                    ram="256Gi",
                    db_size="2Ti",
                    pg_version="18",
                    duty_db=duty,
                )
                self.assertEqual(target, config["default_statistics_target"])

        constrained = self.make_conf(
            cpu="2",
            ram="4Gi",
            db_size="2Ti",
            pg_version="18",
            duty_db=DutyDB.STATISTIC,
        )
        self.assertEqual("500", constrained["default_statistics_target"])

    def test_database_size_must_be_a_positive_iec_size(self):
        for value in ("", "0Gi", "-1Gi", 1024):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.make_conf(pg_version="18", db_size=value)

    def test_parallel_planner_costs_and_thresholds_follow_duty(self):
        expected = {
            DutyDB.FINANCIAL: ("2000", "0.15", "32MB", "2MB"),
            DutyDB.OLTP: ("1500", "0.12", "16MB", "1MB"),
            DutyDB.MIXED: ("1000", "0.1", "8MB", "512kB"),
            DutyDB.STATISTIC: ("500", "0.05", "4MB", "256kB"),
        }
        for duty, values in expected.items():
            with self.subTest(duty=duty):
                config = self.make_conf(pg_version="18", duty_db=duty)
                self.assertEqual(values[0], config["parallel_setup_cost"])
                self.assertEqual(values[1], config["parallel_tuple_cost"])
                self.assertEqual(values[2], config["min_parallel_table_scan_size"])
                self.assertEqual(values[3], config["min_parallel_index_scan_size"])

        pg96 = self.make_conf(pg_version="9.6", duty_db=DutyDB.OLTP)
        pg10 = self.make_conf(pg_version="10", duty_db=DutyDB.OLTP)
        self.assertEqual("16MB", pg96["min_parallel_relation_size"])
        self.assertNotIn("min_parallel_table_scan_size", pg96)
        self.assertNotIn("min_parallel_relation_size", pg10)
        self.assertEqual("16MB", pg10["min_parallel_table_scan_size"])

    def test_jit_policy_is_duty_and_version_aware(self):
        for duty in (DutyDB.FINANCIAL, DutyDB.OLTP):
            with self.subTest(duty=duty):
                self.assertEqual("off", self.make_conf(pg_version="18", duty_db=duty)["jit"])

        mixed = self.make_conf(pg_version="18", duty_db=DutyDB.MIXED)
        statistic = self.make_conf(pg_version="18", duty_db=DutyDB.STATISTIC)
        self.assertEqual("on", mixed["jit"])
        self.assertEqual("on", statistic["jit"])
        self.assertGreater(float(mixed["jit_above_cost"]), float(statistic["jit_above_cost"]))
        self.assertNotIn("jit", self.make_conf(pg_version="10"))

    def test_new_io_execution_limits_follow_major_platform_and_duty(self):
        self.assertNotIn("vacuum_buffer_usage_limit", self.make_conf(pg_version="15"))
        pg16 = self.make_conf(pg_version="16", duty_db=DutyDB.STATISTIC)
        pg17 = self.make_conf(pg_version="17", duty_db=DutyDB.STATISTIC, disk_score=100)
        pg18 = self.make_conf(pg_version="18", duty_db=DutyDB.STATISTIC, disk_score=100)
        windows = self.make_conf(
            pg_version="18",
            duty_db=DutyDB.STATISTIC,
            disk_score=100,
            platform=Platform.WINDOWS,
        )

        self.assertEqual("32MB", pg16["vacuum_buffer_usage_limit"])
        self.assertEqual("256kB", pg17["io_combine_limit"])
        self.assertNotIn("io_max_combine_limit", pg17)
        self.assertEqual("1024kB", pg18["io_combine_limit"])
        self.assertEqual("1024kB", pg18["io_max_combine_limit"])
        self.assertEqual("128kB", windows["io_combine_limit"])
        self.assertEqual("128kB", windows["io_max_combine_limit"])

    def test_windows_before_18_writes_no_read_ahead_hints(self):
        # Before 18 effective_io_concurrency and maintenance_io_concurrency are
        # posix_fadvise hints; a Windows build has no posix_fadvise and refuses
        # any value but 0 at startup. 18 issues its own asynchronous I/O.
        for version in ("9.6", "12", "13", "17"):
            with self.subTest(version=version):
                windows = self.make_conf(
                    pg_version=version, platform=Platform.WINDOWS, disk_type=DiskType.NVME
                )
                self.assertEqual("0", windows["effective_io_concurrency"])
                if "maintenance_io_concurrency" in windows:
                    self.assertEqual("0", windows["maintenance_io_concurrency"])
                linux = self.make_conf(pg_version=version, disk_type=DiskType.NVME)
                self.assertEqual("256", linux["effective_io_concurrency"])

        windows_18 = self.make_conf(
            pg_version="18", platform=Platform.WINDOWS, disk_type=DiskType.NVME
        )
        self.assertEqual("256", windows_18["effective_io_concurrency"])
        self.assertEqual("64", windows_18["maintenance_io_concurrency"])

    def test_synchronous_standby_keywords_follow_the_major(self):
        # ANY and FIRST arrived in 10; 9.6 knows a bare list and a count.
        for value in ("ANY 1 (a, b)", "first 1 (a)"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as caught:
                    self.make_conf(pg_version="9.6", synchronous_standby_names=value)
                self.assertIn("PostgreSQL 10 introduced", str(caught.exception))
        self.assertEqual(
            "'1 (a, b)'",
            self.make_conf(pg_version="9.6", synchronous_standby_names="1 (a, b)")[
                "synchronous_standby_names"
            ],
        )
        self.assertEqual(
            "'ANY 1 (a, b)'",
            self.make_conf(pg_version="10", synchronous_standby_names="ANY 1 (a, b)")[
                "synchronous_standby_names"
            ],
        )

    def test_configuration_string_values_are_escaped_and_round_trip(self):
        raw_value = r"standby'one\west"
        encoded = quote_postgresql_conf_value(raw_value)

        self.assertEqual(r"'standby''one\\west'", encoded)
        self.assertEqual(raw_value, unquote_postgresql_conf_value(encoded))
        self.assertEqual(
            encoded,
            self.make_conf(pg_version="18", synchronous_standby_names=raw_value)[
                "synchronous_standby_names"
            ],
        )

    def test_synchronous_standby_names_rejects_configuration_line_breaks_and_nul(self):
        for separator in ("\n", "\r", "\x00"):
            with self.subTest(separator=repr(separator)):
                with self.assertRaisesRegex(
                    ValueError,
                    "synchronous_standby_names must not contain NUL or line breaks",
                ):
                    self.make_conf(
                        pg_version="18",
                        synchronous_standby_names=f"standby1'{separator}fsync = off",
                    )

    def test_profile_1c_applies_transactional_planner_and_maintenance_contract(self):
        extensions = "auto_explain,online_analyze,pg_stat_statements,pg_store_plans,plantuner"
        for version in PGConfigurator.known_versions:
            with self.subTest(version=version):
                config = self.make_conf(
                    cpu="32",
                    ram="256Gi",
                    db_size="2Ti",
                    pg_version=version,
                    duty_db=DutyDB.STATISTIC,
                    conf_profiles="profile_1c",
                    available_extensions=extensions,
                )
                self.assertEqual("5000", config["default_statistics_target"])
                self.assertEqual("off", config["enable_mergejoin"])
                self.assertEqual("0", config["max_parallel_workers_per_gather"])
                self.assertGreaterEqual(int(config["max_locks_per_transaction"]), 512)
                self.assertEqual("20ms", config["bgwriter_delay"])
                self.assertEqual("400", config["bgwriter_lru_maxpages"])
                self.assertEqual("8000", config["max_files_per_process"])
                self.assertEqual("off", config["online_analyze.enable"])
                self.assertEqual("50", config["online_analyze.threshold"])
                if version not in {"9.6", "10"}:
                    self.assertEqual("off", config["jit"])

    def test_profile_1c_is_exclusive_and_requires_four_autovacuum_workers(self):
        for profiles in (
            "profile_1c,ext_perf",
            "ext_perf,profile_1c",
            "profile_1c,profile_backend_common",
            "profile_backend_perf,profile_1c",
        ):
            with self.subTest(profiles=profiles):
                with self.assertRaisesRegex(ValueError, "exclusive compatibility profile"):
                    self.make_conf(pg_version="18", conf_profiles=profiles)

        with self.assertRaisesRegex(ValueError, "at least 4"):
            self.make_conf(
                pg_version="18",
                conf_profiles="profile_1c",
                min_autovac_workers=3,
                max_autovac_workers=3,
            )

        combined_backend = self.make_conf(
            pg_version="18",
            conf_profiles="profile_backend_common,profile_backend_perf",
        )
        self.assertEqual("on", combined_backend["online_analyze.enable"])
        self.assertEqual("try", combined_backend["huge_pages"])

    def test_memory_envelope_stays_below_safety_limit(self):
        scenarios = (
            ("1", "1Gi", None),
            ("8", "2Gi", "profile_1c"),
            ("16", "64Gi", None),
            ("96", "768Gi", "profile_backend_perf"),
        )
        for cpu, ram, profile in scenarios:
            with self.subTest(cpu=cpu, ram=ram, profile=profile):
                self.make_conf(cpu=cpu, ram=ram, pg_version="18", conf_profiles=profile)
                calculation = self.configurator.last_calculation
                self.assertLessEqual(
                    calculation["memory_envelope_bytes"],
                    calculation["available_ram_bytes"] * 0.9,
                )

    def test_apply_modes_come_from_pg_settings_context(self):
        self.assertEqual("reload_and_reconnect", PGConfigurator._apply_mode_for_context("backend"))
        self.assertEqual(
            "reload_and_reconnect",
            PGConfigurator._apply_mode_for_context("superuser-backend"),
        )
        self.assertEqual("immutable", PGConfigurator._apply_mode_for_context("internal"))
        self.assertEqual("manual", PGConfigurator._apply_mode_for_context("unknown"))

        self.make_conf(pg_version="18", common_conf=True)
        details = self.configurator.last_parameter_details

        self.assertEqual("postmaster", details["logging_collector"]["context"])
        self.assertEqual("restart", details["logging_collector"]["apply_mode"])
        self.assertEqual("sighup", details["io_workers"]["context"])
        self.assertEqual("reload", details["io_workers"]["apply_mode"])
        self.assertEqual("postmaster", details["max_logical_replication_workers"]["context"])
        self.assertEqual("constant", details["logging_collector"]["rule_kind"])
        self.assertIsNone(details["logging_collector"]["rule"])
        self.assertEqual("expression", details["io_workers"]["rule_kind"])
        self.assertEqual("pg_settings_snapshot", details["io_workers"]["context_source"])

        self.make_conf(pg_version="18", conf_profiles="profile_backend_common")
        external = self.configurator.last_parameter_details["online_analyze.enable"]
        self.assertEqual("unknown", external["context"])
        self.assertEqual("external_extension", external["context_source"])
        self.assertEqual("manual", external["apply_mode"])

    def test_unknown_extension_setting_is_rejected(self):
        configurator = PGConfigurator(
            self.args, [{"name": "typo_extension.unknown_setting", "const": "on"}]
        )

        with self.assertRaisesRegex(ValueError, "Extension parameters were not declared"):
            with redirect_stdout(StringIO()):
                configurator.make_conf("8", "16Gi", pg_version="18")

    def test_snapshot_bounds_reject_out_of_range_external_override(self):
        configurator = PGConfigurator(self.args, [{"name": "max_connections", "const": "99999999"}])

        with self.assertRaisesRegex(ValueError, "max_val"):
            with redirect_stdout(StringIO()):
                configurator.make_conf("8", "16Gi", pg_version="18")

    def test_parallel_workers_scale_with_cpu_and_memory(self):
        constrained = self.make_conf(cpu="1", ram="1Gi", pg_version="18")
        constrained_active_sessions = self.configurator.last_calculation["active_query_sessions"]
        small = self.make_conf(cpu="2", ram="2Gi", pg_version="18")
        large = self.make_conf(cpu="96", ram="768Gi", pg_version="18")

        self.assertEqual(4, constrained_active_sessions)
        self.assertEqual("0", constrained["max_parallel_workers"])
        self.assertEqual("0", constrained["max_parallel_workers_per_gather"])
        self.assertEqual("0", constrained["max_parallel_maintenance_workers"])
        self.assertLess(int(small["max_parallel_workers"]), int(large["max_parallel_workers"]))
        self.assertLess(
            int(small["max_parallel_workers_per_gather"]),
            int(large["max_parallel_workers_per_gather"]),
        )
        self.assertLess(
            int(small["max_parallel_maintenance_workers"]),
            int(large["max_parallel_maintenance_workers"]),
        )

    def test_lock_budgets_scale_safely_on_powerful_servers(self):
        small = self.make_conf(cpu="4", ram="4Gi", pg_version="18")
        large = self.make_conf(cpu="96", ram="768Gi", pg_version="18")

        for setting in (
            "max_locks_per_transaction",
            "max_pred_locks_per_transaction",
            "max_pred_locks_per_page",
            "max_pred_locks_per_relation",
        ):
            with self.subTest(setting=setting):
                self.assertLess(int(small[setting]), int(large[setting]))
        self.assertLessEqual(int(large["max_locks_per_transaction"]), 2048)
        self.assertLessEqual(int(large["max_pred_locks_per_transaction"]), 1024)

    def test_profile_aware_timeouts_are_present(self):
        financial = self.make_conf(pg_version="18", duty_db=DutyDB.FINANCIAL)
        oltp = self.make_conf(pg_version="18", duty_db=DutyDB.OLTP)
        mixed = self.make_conf(pg_version="18", duty_db=DutyDB.MIXED)
        statistic = self.make_conf(pg_version="18", duty_db=DutyDB.STATISTIC)

        for config in (financial, oltp, mixed, statistic):
            self.assertIn("lock_timeout", config)
            self.assertIn("statement_timeout", config)
            self.assertIn("idle_in_transaction_session_timeout", config)
            self.assertIn("idle_session_timeout", config)
            self.assertIn("transaction_timeout", config)
        self.assertEqual("5s", financial["lock_timeout"])
        self.assertEqual("10s", oltp["lock_timeout"])
        self.assertEqual("15s", mixed["lock_timeout"])
        self.assertEqual("1min", statistic["lock_timeout"])
        self.assertEqual("5min", financial["statement_timeout"])
        self.assertEqual("15min", oltp["statement_timeout"])
        self.assertEqual("30min", mixed["statement_timeout"])
        self.assertEqual("4h", statistic["statement_timeout"])

    def test_network_timeouts_are_platform_duty_and_version_aware(self):
        financial_linux = self.make_conf(
            pg_version="18",
            duty_db=DutyDB.FINANCIAL,
            platform=Platform.LINUX,
        )
        oltp_linux = self.make_conf(
            pg_version="18",
            duty_db=DutyDB.OLTP,
            platform=Platform.LINUX,
        )
        statistic_linux = self.make_conf(
            pg_version="18",
            duty_db=DutyDB.STATISTIC,
            platform=Platform.LINUX,
        )
        financial_windows = self.make_conf(
            pg_version="18",
            duty_db=DutyDB.FINANCIAL,
            platform=Platform.WINDOWS,
        )

        self.assertEqual("60s", financial_linux["tcp_keepalives_idle"])
        self.assertEqual("10s", financial_linux["tcp_keepalives_interval"])
        self.assertEqual("6", financial_linux["tcp_keepalives_count"])
        self.assertEqual("120s", financial_linux["tcp_user_timeout"])
        self.assertEqual("5s", financial_linux["client_connection_check_interval"])
        self.assertEqual("60s", financial_linux["wal_sender_timeout"])
        self.assertEqual("90s", oltp_linux["tcp_keepalives_idle"])
        self.assertEqual("15s", oltp_linux["tcp_keepalives_interval"])
        self.assertEqual("6", oltp_linux["tcp_keepalives_count"])
        self.assertEqual("180s", oltp_linux["tcp_user_timeout"])
        self.assertEqual("10s", oltp_linux["client_connection_check_interval"])
        self.assertEqual("90s", oltp_linux["wal_sender_timeout"])
        self.assertEqual("300s", statistic_linux["wal_sender_timeout"])
        self.assertEqual("30s", statistic_linux["client_connection_check_interval"])

        self.assertEqual("0", financial_windows["tcp_keepalives_count"])
        self.assertEqual("0", financial_windows["tcp_user_timeout"])
        self.assertEqual("0", financial_windows["client_connection_check_interval"])

        pg11 = self.make_conf(pg_version="11")
        pg12 = self.make_conf(pg_version="12")
        pg14 = self.make_conf(pg_version="14")
        pg16 = self.make_conf(pg_version="16")
        self.assertNotIn("tcp_user_timeout", pg11)
        self.assertIn("tcp_user_timeout", pg12)
        self.assertEqual("TLSv1.2", pg12["ssl_min_protocol_version"])
        self.assertNotIn("client_connection_check_interval", pg12)
        self.assertIn("client_connection_check_interval", pg14)
        self.assertIn("reserved_connections", pg16)

    def test_authentication_defaults_upgrade_to_scram_when_supported(self):
        pg96 = self.make_conf(pg_version="9.6")
        pg10 = self.make_conf(pg_version="10")

        self.assertEqual("on", pg96["password_encryption"])
        self.assertEqual("scram-sha-256", pg10["password_encryption"])
        self.assertEqual("30s", pg10["authentication_timeout"])

    def test_oltp_duty_has_a_distinct_transactional_composition(self):
        financial = self.make_conf(cpu="16", ram="64Gi", pg_version="18", duty_db="financial")
        oltp = self.make_conf(cpu="16", ram="64Gi", pg_version="18", duty_db="oltp")
        oltp_calculation = self.configurator.last_calculation.copy()
        mixed = self.make_conf(cpu="16", ram="64Gi", pg_version="18", duty_db="mixed")

        self.assertGreater(int(oltp["max_connections"]), int(mixed["max_connections"]))
        self.assertGreater(
            UnitConverter.size_from(oltp["work_mem"], UnitConverter.sys_pg),
            UnitConverter.size_from(financial["work_mem"], UnitConverter.sys_pg),
        )
        self.assertLessEqual(
            UnitConverter.size_from(oltp["work_mem"], UnitConverter.sys_pg),
            32 * 1024**2,
        )
        self.assertLess(
            int(oltp["max_parallel_workers"]),
            int(mixed["max_parallel_workers"]),
        )
        self.assertEqual("10min", oltp["checkpoint_timeout"])
        self.assertEqual("20s", oltp["autovacuum_naptime"])
        self.assertEqual("0.015", oltp["autovacuum_vacuum_scale_factor"])
        self.assertEqual("0.0075", oltp["autovacuum_analyze_scale_factor"])
        self.assertEqual("on", oltp["synchronous_commit"])
        self.assertEqual(5, oltp_calculation["connections_per_cpu"])
        self.assertEqual(32 * 1024**2, oltp_calculation["work_mem_cap_bytes"])

    def test_oltp_duty_matches_every_major_and_bundled_profile(self):
        for version in PGConfigurator.known_versions:
            for profile in (None, *PGConfigurator.conf_profiles):
                with self.subTest(version=version, profile=profile):
                    self.make_conf(
                        pg_version=version,
                        duty_db=DutyDB.OLTP,
                        conf_profiles=profile,
                    )

    def test_logical_subscription_inputs_and_memory_are_version_aware(self):
        with self.assertRaisesRegex(ValueError, "PostgreSQL 10 or newer"):
            self.make_conf(
                pg_version="9.6",
                replication_mode=ReplicationMode.LOGICAL,
                logical_subscription_count=1,
            )
        with self.assertRaisesRegex(ValueError, "replication_mode=logical"):
            self.make_conf(
                pg_version="18",
                replication_mode=ReplicationMode.PHYSICAL,
                logical_subscription_count=1,
            )

        self.make_conf(
            pg_version="18",
            replication_mode=ReplicationMode.LOGICAL,
            logical_subscription_count=2,
        )
        self.assertGreater(
            self.configurator.last_calculation["logical_decoding_memory_envelope_bytes"],
            0,
        )

    def test_wal_segment_size_and_disk_budget_are_consistent(self):
        with self.assertRaisesRegex(ValueError, "at least 1Gi"):
            self.make_conf(
                pg_version="18",
                wal_segment_size="16Mi",
                wal_disk_budget="512Mi",
            )
        with self.assertRaisesRegex(ValueError, "between 1Mi and 1Gi"):
            self.make_conf(pg_version="18", wal_segment_size="3Mi")
        with self.assertRaisesRegex(ValueError, "at least eight WAL segments"):
            self.make_conf(
                pg_version="18",
                wal_segment_size="1Gi",
                wal_disk_budget="4Gi",
            )

        config = self.make_conf(
            pg_version="18",
            wal_segment_size="1Gi",
            wal_disk_budget="32Gi",
        )
        self.assertGreaterEqual(
            UnitConverter.size_from(config["min_wal_size"], UnitConverter.sys_pg),
            2 * 1024**3,
        )

        minimum_retention = self.make_conf(
            pg_version="18",
            peak_wal_rate="1Mi",
            replica_outage_tolerance=0,
            wal_disk_budget="32Gi",
        )
        self.assertEqual(
            512 * 1024**2,
            UnitConverter.size_from(minimum_retention["wal_keep_size"], UnitConverter.sys_pg),
        )

    def test_common_logging_and_extensions_are_enabled_by_default(self):
        config = self.make_conf(pg_version="18")

        self.assertEqual("'csvlog'", config["log_destination"])
        self.assertEqual("on", config["logging_collector"])
        self.assertEqual("10MB", config["log_temp_files"])
        self.assertIn("pg_stat_statements", config["shared_preload_libraries"])
        self.assertIn("auto_explain", config["shared_preload_libraries"])
        self.assertIn("auto_explain.log_min_duration", config)
        self.assertIn("pg_stat_statements.track", config)

        details = self.configurator.last_parameter_details
        for setting in (
            "logging_collector",
            "auto_explain.log_min_duration",
            "pg_stat_statements.track",
        ):
            self.assertTrue(details[setting]["source"].startswith("common"))

    def test_observability_sampling_is_bounded_by_workload_duty(self):
        financial = self.make_conf(pg_version="18", duty_db=DutyDB.FINANCIAL)
        oltp = self.make_conf(pg_version="18", duty_db=DutyDB.OLTP)
        mixed = self.make_conf(pg_version="18", duty_db=DutyDB.MIXED)
        statistic = self.make_conf(pg_version="18", duty_db=DutyDB.STATISTIC)

        for setting in (
            "auto_explain.sample_rate",
            "log_statement_sample_rate",
            "log_transaction_sample_rate",
        ):
            with self.subTest(setting=setting):
                self.assertLess(float(financial[setting]), float(oltp[setting]))
                self.assertLess(float(oltp[setting]), float(mixed[setting]))
                self.assertLess(float(mixed[setting]), float(statistic[setting]))

    def test_common_extension_rules_follow_major_version_capabilities(self):
        pg11 = self.make_conf(pg_version="11")
        pg12 = self.make_conf(pg_version="12")
        pg13 = self.make_conf(pg_version="13")
        pg16 = self.make_conf(pg_version="16")

        self.assertNotIn("auto_explain.log_level", pg11)
        self.assertIn("auto_explain.log_level", pg12)
        self.assertIn("log_transaction_sample_rate", pg12)
        self.assertNotIn("auto_explain.log_wal", pg12)
        self.assertIn("auto_explain.log_wal", pg13)
        self.assertIn("pg_stat_statements.track_planning", pg13)
        self.assertIn("log_min_duration_sample", pg13)
        self.assertEqual("0", pg13["log_parameter_max_length"])
        self.assertEqual("0", pg13["log_parameter_max_length_on_error"])
        self.assertNotIn("auto_explain.log_parameter_max_length", pg13)
        self.assertEqual("0", pg16["auto_explain.log_parameter_max_length"])

    def test_profile_preload_is_assembled_without_losing_common_extensions(self):
        extensions = "auto_explain,online_analyze,pg_stat_statements,pg_store_plans,plantuner"
        config = self.make_conf(
            pg_version="18",
            conf_profiles="profile_1c",
            available_extensions=extensions,
        )

        preloaded = set(config["shared_preload_libraries"].strip("'").split(","))
        self.assertEqual(set(extensions.split(",")), preloaded)
        self.assertEqual(
            "common:profile_1c",
            self.configurator.last_parameter_details["online_analyze.enable"]["source"],
        )

    def test_fractional_size_values_are_parsed_correctly(self):
        self.assertEqual(
            int(1.5 * 1024**3), UnitConverter.size_from("1.5Gi", system=UnitConverter.sys_iec)
        )


if __name__ == "__main__":
    unittest.main()
