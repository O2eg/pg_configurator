"""What the advisories say must be true of the file that was generated.

The parity suite compares Python against the JavaScript port, so a statement
that is wrong in both passes it. These tests read the generated configuration
instead: every advisory that names a setting has to quote the value that setting
ends up with, and every list of settings has to contain only settings the target
version actually has.

That is the shape of the bug this file exists to prevent. The advisories were
once built before the profile rules ran, so they described a draft: they
promised synchronous_commit=on for a cluster whose file says remote_apply,
called a statistics target high after a profile had lowered it, and named
timeouts that PostgreSQL 12 has never had.
"""

import unittest
from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace

from pg_configurator.configurator import (
    ADVISORY_SEVERITIES,
    PGConfigurator,
    UnitConverter,
)

MEBIBYTE = 1024**2


def generate(cpu="8", ram="16Gi", **kwargs):
    """Return the configuration and the advisories that describe it."""
    configurator = PGConfigurator(
        SimpleNamespace(output_file_name="", debug_mode=False),
        {},
    )
    with redirect_stdout(StringIO()):
        config = configurator.make_conf(cpu, ram, **kwargs)
    return config, configurator


def by_code(configurator):
    return {item["code"]: item for item in configurator.last_advisories}


def settings_named(message, candidates):
    """The settings a message actually lists, in the order it lists them."""
    return [name for name in candidates if f"{name}=" in message]


class TestAdvisoryShape(unittest.TestCase):
    def test_codes_are_unique_and_severities_are_declared(self):
        for arguments in (
            {"pg_version": "18"},
            {"pg_version": "9.6", "platform": "WINDOWS"},
            {"pg_version": "18", "conf_profiles": "profile_1c"},
        ):
            with self.subTest(**arguments):
                _, configurator = generate(**arguments)
                codes = [item["code"] for item in configurator.last_advisories]
                self.assertEqual(sorted(set(codes)), sorted(codes))
                for item in configurator.last_advisories:
                    self.assertIn(item["severity"], ADVISORY_SEVERITIES)
                    self.assertTrue(item["message"].strip())
                    self.assertEqual(item["message"], item["message"].strip())

    def test_a_named_setting_is_generated_and_quoted_as_generated(self):
        # The generic guard against describing a draft: whatever an advisory
        # says a setting is, the file has to agree.
        for arguments in (
            {"pg_version": "18"},
            {"pg_version": "12"},
            {"pg_version": "18", "conf_profiles": "profile_1c"},
            {
                "pg_version": "18",
                "conf_profiles": "profile_1c",
                "duty_db": "financial",
                "replication_mode": "physical",
                "synchronous_standby_names": "ANY 1 (standby)",
            },
            {"pg_version": "18", "platform": "WINDOWS"},
            {"pg_version": "18", "replication_mode": "logical", "pitr_enabled": True},
        ):
            with self.subTest(**arguments):
                config, configurator = generate(**arguments)
                for item in configurator.last_advisories:
                    if item["setting"] is None:
                        continue
                    self.assertIn(item["setting"], config, item["code"])
                    self.assertEqual(config[item["setting"]], item["actual"], item["code"])
                    self.assertIn(f"{item['setting']}={item['actual']}", item["message"])

    def test_severest_comes_first(self):
        _, configurator = generate(pg_version="9.6", conf_profiles="profile_1c")
        ranks = [
            ADVISORY_SEVERITIES.index(item["severity"]) for item in configurator.last_advisories
        ]
        self.assertEqual(sorted(ranks), ranks)

    def test_an_ordinary_configuration_raises_no_warning(self):
        # Ten findings on a healthy configuration is how a reader learns to
        # skip them. Only a real risk or conflict may claim that word.
        _, configurator = generate(pg_version="18")
        warnings = [item for item in configurator.last_advisories if item["severity"] == "warning"]
        self.assertEqual([], warnings)


class TestAdvisoriesDescribeTheFinalConfiguration(unittest.TestCase):
    def test_profile_1c_quotes_the_synchronous_commit_it_ended_up_with(self):
        config, configurator = generate(
            pg_version="18",
            conf_profiles="profile_1c",
            duty_db="financial",
            replication_mode="physical",
            synchronous_standby_names="ANY 1 (standby)",
        )
        item = by_code(configurator)["profile_1c_synchronous_commit"]

        self.assertEqual("remote_apply", config["synchronous_commit"])
        self.assertEqual("remote_apply", item["actual"])
        self.assertNotIn("synchronous_commit=on", item["message"])

    def test_a_high_statistics_target_is_judged_after_the_profile_lowered_it(self):
        config, configurator = generate(
            cpu="16",
            ram="128Gi",
            pg_version="18",
            conf_profiles="profile_1c",
            duty_db="statistic",
        )
        self.assertEqual("1000", config["default_statistics_target"])
        self.assertNotIn("high_statistics_target", by_code(configurator))

        config, configurator = generate(cpu="16", ram="128Gi", pg_version="18", duty_db="statistic")
        self.assertEqual("2500", config["default_statistics_target"])
        self.assertIn("high_statistics_target", by_code(configurator))

    def test_the_work_mem_advisory_counts_the_sessions_the_calculation_reports(self):
        _, configurator = generate(
            cpu="1", ram="16Gi", pg_version="18", conf_profiles="profile_1c", min_conns=1
        )
        sessions = configurator.last_calculation["active_query_sessions"]
        item = by_code(configurator)["work_mem_budget_assumption"]

        self.assertEqual(4, sessions)
        self.assertIn(f"{sessions} concurrent sessions", item["message"])

    def test_the_work_mem_advisory_names_the_hash_reserve_it_actually_used(self):
        # The budget is divided by a hash multiplier on every major. Before 13
        # there is no such GUC in the file, so the message has to say where the
        # number comes from instead of quoting a setting that is not there.
        config, configurator = generate(pg_version="18")
        self.assertIn(
            f"hash_mem_multiplier={config['hash_mem_multiplier']}",
            by_code(configurator)["work_mem_budget_assumption"]["message"],
        )

        config, configurator = generate(pg_version="12")
        message = by_code(configurator)["work_mem_budget_assumption"]["message"]
        self.assertNotIn("hash_mem_multiplier", config)
        self.assertIn("a factor of 2.0 for hash operations", message)

    def test_profile_1c_names_the_limit_that_actually_bound_the_connections(self):
        for arguments, expected in (
            ({"max_conns": 100}, "max_conns=100"),
            ({"cpu": "1", "min_conns": 1}, "the memory budget"),
            ({"cpu": "192", "ram": "1Ti", "max_conns": 2000}, "its own request of 1000"),
        ):
            with self.subTest(**arguments):
                config, configurator = generate(
                    pg_version="18", conf_profiles="profile_1c", **arguments
                )
                item = by_code(configurator)["profile_1c_connection_target"]
                self.assertEqual(config["max_connections"], item["actual"])
                self.assertIn(expected, item["message"])

    def test_row_security_off_is_not_described_as_a_bypass(self):
        _, configurator = generate(pg_version="18", conf_profiles="profile_1c")
        message = by_code(configurator)["profile_1c_row_security_disabled"]["message"]

        # row_security=off raises an error in lieu of applying a policy; it does
        # not read the rows a policy would have hidden.
        self.assertIn("fails with an error", message)
        self.assertIn("BYPASSRLS", message)


class TestVersionAwareAdvisories(unittest.TestCase):
    timeout_settings = (
        "statement_timeout",
        "lock_timeout",
        "idle_in_transaction_session_timeout",
        "idle_session_timeout",
        "transaction_timeout",
    )
    network_settings = (
        "tcp_keepalives_idle",
        "tcp_keepalives_interval",
        "tcp_keepalives_count",
        "tcp_user_timeout",
        "client_connection_check_interval",
    )

    def test_the_timeout_advisory_lists_exactly_the_timeouts_that_exist(self):
        for version in PGConfigurator.known_versions:
            with self.subTest(version=version):
                config, configurator = generate(pg_version=version)
                item = by_code(configurator)["workload_timeouts_are_instance_wide"]
                self.assertEqual(
                    [name for name in self.timeout_settings if name in config],
                    settings_named(item["message"], self.timeout_settings),
                )

    def test_the_network_advisory_lists_exactly_the_options_that_exist(self):
        for version in PGConfigurator.known_versions:
            for platform in ("LINUX", "WINDOWS"):
                with self.subTest(version=version, platform=platform):
                    config, configurator = generate(pg_version=version, platform=platform)
                    item = by_code(configurator)["network_timeouts_are_baselines"]
                    self.assertEqual(
                        [name for name in self.network_settings if name in config],
                        settings_named(item["message"], self.network_settings),
                    )

    def test_windows_only_describes_zeros_the_version_actually_writes(self):
        config, configurator = generate(pg_version="9.6", platform="WINDOWS")
        message = by_code(configurator)["windows_network_options_unavailable"]["message"]
        self.assertNotIn("tcp_user_timeout", config)
        self.assertNotIn("tcp_user_timeout", message)
        self.assertNotIn("client_connection_check_interval", message)

        config, configurator = generate(pg_version="18", platform="WINDOWS")
        message = by_code(configurator)["windows_network_options_unavailable"]["message"]
        self.assertEqual("0", config["client_connection_check_interval"])
        # Zero there is the check switched off, not an operating-system default.
        self.assertIn("not a system default", message)

    def test_the_pooler_advisory_follows_the_setting_rather_than_a_version_list(self):
        for version in PGConfigurator.known_versions:
            with self.subTest(version=version):
                config, configurator = generate(pg_version=version)
                self.assertEqual(
                    "idle_session_timeout" in config,
                    "idle_session_timeout_and_poolers" in by_code(configurator),
                )

    def test_end_of_life_is_read_from_the_dated_table(self):
        self.assertEqual(
            sorted(PGConfigurator.known_versions), sorted(PGConfigurator.postgresql_eol_dates)
        )
        for version, end_of_life in PGConfigurator.postgresql_eol_dates.items():
            with self.subTest(version=version):
                _, configurator = generate(pg_version=version)
                codes = by_code(configurator)
                expired = end_of_life <= PGConfigurator.support_horizon
                self.assertEqual(expired, "postgresql_end_of_life" in codes)
                if expired:
                    self.assertIn(end_of_life, codes["postgresql_end_of_life"]["message"])


class TestAdvisoriesForHardwareAndVersionShapes(unittest.TestCase):
    """The advisories added after the matrix review of 2026-09-03.

    Each one is tied to a shape of hardware, replication or PostgreSQL major
    that the earlier set said nothing about. The rule is unchanged: a setting
    named is quoted as generated, and a version-specific sentence appears only
    on the majors it is true for.
    """

    def test_windows_before_18_explains_the_zeroed_read_ahead(self):
        for version in ("9.6", "13", "17"):
            with self.subTest(version=version):
                config, configurator = generate(pg_version=version, platform="WINDOWS")
                item = by_code(configurator)["windows_io_prefetch_unavailable"]
                self.assertEqual("0", item["actual"])
                self.assertEqual(
                    "maintenance_io_concurrency" in config,
                    "maintenance_io_concurrency=0" in item["message"],
                )
        _, configurator = generate(pg_version="18", platform="WINDOWS")
        self.assertNotIn("windows_io_prefetch_unavailable", by_code(configurator))
        _, configurator = generate(pg_version="17")
        self.assertNotIn("windows_io_prefetch_unavailable", by_code(configurator))

    def test_an_assumed_peak_wal_rate_is_declared_and_a_supplied_one_is_not(self):
        config, configurator = generate(pg_version="18")
        item = by_code(configurator)["peak_wal_rate_assumed"]
        self.assertEqual("4Mi", item["actual"])
        self.assertIn(f"max_wal_size={config['max_wal_size']}", item["message"])
        self.assertIn(f"wal_keep_size={config['wal_keep_size']}", item["message"])
        self.assertEqual("default", configurator.last_inputs["peak_wal_rate_source"])

        _, configurator = generate(pg_version="18", peak_wal_rate="4Mi")
        self.assertNotIn("peak_wal_rate_assumed", by_code(configurator))
        self.assertEqual("explicit", configurator.last_inputs["peak_wal_rate_source"])

        # Without replication the retention setting is not something the peak sizes.
        _, configurator = generate(pg_version="18", replication_mode="none")
        self.assertNotIn("wal_keep_size", by_code(configurator)["peak_wal_rate_assumed"]["message"])

    def test_max_wal_size_capped_by_the_budget_reports_seconds_at_peak(self):
        config, configurator = generate(
            pg_version="14", wal_disk_budget="8Gi", peak_wal_rate="50Mi"
        )
        item = by_code(configurator)["max_wal_size_capped_by_wal_budget"]
        self.assertEqual("warning", item["severity"])
        self.assertEqual("4096MB", config["max_wal_size"])
        self.assertEqual(config["max_wal_size"], item["actual"])
        # 4096 MiB at 50 MiB/s: 81.92 seconds, reported whole.
        self.assertIn("about 81s of peak writes", item["message"])
        self.assertIn(f"checkpoint_timeout={config['checkpoint_timeout']}", item["message"])

        _, configurator = generate(pg_version="14")
        self.assertNotIn("max_wal_size_capped_by_wal_budget", by_code(configurator))

    def test_the_work_mem_worst_case_is_measured_against_physical_ram(self):
        config, configurator = generate(
            cpu="4", ram="8Gi", pg_version="16", conf_profiles="profile_1c"
        )
        item = by_code(configurator)["work_mem_worst_case_exceeds_ram"]
        self.assertEqual("warning", item["severity"])
        self.assertEqual(config["work_mem"], item["actual"])
        self.assertIn(f"max_connections={config['max_connections']}", item["message"])
        self.assertIn(f"hash_mem_multiplier={config['hash_mem_multiplier']}", item["message"])
        self.assertIn("against 8192MB of RAM", item["message"])

        _, configurator = generate(pg_version="18")
        self.assertNotIn("work_mem_worst_case_exceeds_ram", by_code(configurator))

        # Before 13 the hash reserve is not a setting, so it is named as a factor.
        config, configurator = generate(
            cpu="4", ram="8Gi", pg_version="12", conf_profiles="profile_1c"
        )
        message = by_code(configurator)["work_mem_worst_case_exceeds_ram"]["message"]
        self.assertNotIn("hash_mem_multiplier", config)
        self.assertIn("a hash factor of 2.0", message)

    def test_the_connection_bound_is_named_only_when_it_bites(self):
        config, configurator = generate(cpu="64", ram="64Gi", pg_version="18", max_conns=100)
        item = by_code(configurator)["max_connections_capped"]
        self.assertEqual("100", config["max_connections"])
        self.assertIn("would give 272", item["message"])
        self.assertIn("max_conns=100 is what holds it down", item["message"])

        config, configurator = generate(cpu="24", ram="4Gi", pg_version="18")
        item = by_code(configurator)["max_connections_capped"]
        capacity = configurator.last_calculation["connection_capacity"]
        self.assertEqual(str(capacity), config["max_connections"])
        self.assertIn(f"the memory budget, which holds {capacity}", item["message"])

        _, configurator = generate(pg_version="18")
        self.assertNotIn("max_connections_capped", by_code(configurator))
        # profile_1c explains its own connection target.
        _, configurator = generate(pg_version="18", conf_profiles="profile_1c", max_conns=100)
        self.assertNotIn("max_connections_capped", by_code(configurator))

    def test_slot_retention_is_called_unbounded_only_before_13(self):
        for version in ("9.6", "12"):
            with self.subTest(version=version):
                config, configurator = generate(pg_version=version, replica_count=2)
                item = by_code(configurator)["replication_slot_retention_unbounded"]
                self.assertEqual("warning", item["severity"])
                self.assertEqual(config["max_replication_slots"], item["actual"])
                self.assertIn(f"wal_keep_segments={config['wal_keep_segments']}", item["message"])
        for version in ("13", "18"):
            with self.subTest(version=version):
                _, configurator = generate(pg_version=version, replica_count=2)
                self.assertNotIn("replication_slot_retention_unbounded", by_code(configurator))
        _, configurator = generate(pg_version="12", replication_mode="none")
        self.assertNotIn("replication_slot_retention_unbounded", by_code(configurator))

    def test_every_named_synchronous_standby_being_required_is_a_warning(self):
        for version, names, example in (
            ("18", "s1", "ANY 1 (s1, s2)"),
            ("16", "FIRST 2 (a, b)", "ANY 1 (s1, s2)"),
            ("9.6", "2 (a, b)", "1 (s1, s2)"),
        ):
            with self.subTest(version=version, names=names):
                config, configurator = generate(pg_version=version, synchronous_standby_names=names)
                item = by_code(configurator)["synchronous_standbys_all_required"]
                self.assertEqual("warning", item["severity"])
                self.assertEqual(config["synchronous_standby_names"], item["actual"])
                self.assertIn(example, item["message"])
                if version == "9.6":
                    self.assertNotIn("ANY", item["message"])
        for names in ("ANY 1 (a, b)", "FIRST 1 (a, b)", "a, b", "*", "1 (a, b)"):
            with self.subTest(names=names):
                _, configurator = generate(pg_version="18", synchronous_standby_names=names)
                self.assertNotIn("synchronous_standbys_all_required", by_code(configurator))

    def test_replica_count_contradictions_are_explained(self):
        config, configurator = generate(pg_version="18", replication_mode="none", replica_count=3)
        item = by_code(configurator)["replica_count_ignored"]
        self.assertEqual("0", config["max_wal_senders"])
        self.assertEqual(config["max_wal_senders"], item["actual"])
        self.assertIn("replica_count=3", item["message"])
        self.assertNotIn("replication_without_replicas", by_code(configurator))

        config, configurator = generate(pg_version="18", replica_count=0)
        item = by_code(configurator)["replication_without_replicas"]
        self.assertEqual(config["max_wal_senders"], item["actual"])
        self.assertIn("replication_mode=physical", item["message"])

        _, configurator = generate(pg_version="18")
        self.assertNotIn("replica_count_ignored", by_code(configurator))
        self.assertNotIn("replication_without_replicas", by_code(configurator))
        _, configurator = generate(
            pg_version="18",
            replication_mode="logical",
            replica_count=0,
            logical_subscription_count=2,
        )
        self.assertNotIn("replication_without_replicas", by_code(configurator))

    def test_huge_pages_are_raised_from_eight_gigabytes_with_version_specific_help(self):
        _, configurator = generate(pg_version="18")
        self.assertNotIn("huge_pages_not_reserved", by_code(configurator))
        for version, present, absent in (
            ("14", "sized from the shared memory", "shared_memory_size_in_huge_pages"),
            ("15", "shared_memory_size_in_huge_pages", "huge_pages_status"),
            ("17", "huge_pages_status", "sized from"),
        ):
            with self.subTest(version=version):
                config, configurator = generate(cpu="16", ram="64Gi", pg_version=version)
                item = by_code(configurator)["huge_pages_not_reserved"]
                self.assertEqual("assumption", item["severity"])
                self.assertEqual(config["shared_buffers"], item["actual"])
                self.assertIn(present, item["message"])
                self.assertNotIn(absent, item["message"])
        _, configurator = generate(cpu="16", ram="64Gi", pg_version="18", platform="WINDOWS")
        message = by_code(configurator)["huge_pages_not_reserved"]["message"]
        self.assertIn("Lock pages in memory", message)
        self.assertNotIn("vm.nr_hugepages", message)

    def test_build_dependent_settings_are_flagged_where_the_file_writes_them(self):
        config, configurator = generate(pg_version="14")
        codes = by_code(configurator)
        self.assertEqual("on", config["wal_compression"])
        self.assertNotIn("wal_compression_build_unknown", codes)
        self.assertNotIn("io_method_worker_assumed", codes)

        config, configurator = generate(pg_version="15")
        item = by_code(configurator)["wal_compression_build_unknown"]
        self.assertEqual("pglz", item["actual"])

        config, configurator = generate(pg_version="18")
        item = by_code(configurator)["io_method_worker_assumed"]
        self.assertEqual(config["io_method"], item["actual"])
        self.assertIn(f"io_workers={config['io_workers']}", item["message"])

        for version, duty, expected in (
            ("10", "mixed", False),
            ("11", "mixed", True),
            ("18", "oltp", False),
            ("18", "statistic", True),
        ):
            with self.subTest(version=version, duty=duty):
                config, configurator = generate(pg_version=version, duty_db=duty)
                self.assertEqual(expected, config.get("jit") == "on")
                self.assertEqual(expected, "jit_requires_llvm_build" in by_code(configurator))

    def test_pitr_mentions_incremental_backups_from_17(self):
        _, configurator = generate(pg_version="16")
        self.assertNotIn(
            "summarize_wal", by_code(configurator)["pitr_transport_not_configured"]["message"]
        )
        _, configurator = generate(pg_version="17")
        self.assertIn(
            "summarize_wal=on", by_code(configurator)["pitr_transport_not_configured"]["message"]
        )

    def test_logical_replication_notes_follow_the_major(self):
        sentences = {
            "12": (),
            "13": ("logical_decoding_work_mem",),
            "17": ("logical_decoding_work_mem", "synchronized_standby_slots"),
            "18": (
                "logical_decoding_work_mem",
                "synchronized_standby_slots",
                "idle_replication_slot_timeout",
            ),
        }
        every = {name for names in sentences.values() for name in names}
        for version, present in sentences.items():
            with self.subTest(version=version):
                _, configurator = generate(
                    pg_version=version, replication_mode="logical", logical_subscription_count=2
                )
                item = by_code(configurator)["logical_replication_provisioned"]
                self.assertEqual("logical", item["actual"])
                self.assertIn("logical_subscription_count=2", item["message"])
                for name in present:
                    self.assertIn(name, item["message"])
                for name in every.difference(present):
                    self.assertNotIn(name, item["message"])
        _, configurator = generate(pg_version="18")
        self.assertNotIn("logical_replication_provisioned", by_code(configurator))

    def test_rejections_name_a_remedy(self):
        with self.assertRaises(ValueError) as caught:
            generate(cpu="500m", ram="512Mi", pg_version="18")
        self.assertIn("less than min_conns=20: add RAM, lower min_conns", str(caught.exception))
        with self.assertRaises(ValueError) as caught:
            generate(cpu="96", ram="8Gi", pg_version="18")
        self.assertIn("exceeds 90% of available RAM: fewer CPU cores", str(caught.exception))


class TestWalRetentionHonoursItsCeiling(unittest.TestCase):
    def test_what_lands_on_disk_stays_inside_the_forty_percent_it_claims(self):
        # PostgreSQL keeps the segment it is writing on top of the request, and
        # converts a byte request down to whole segments. Rounding up and then
        # calling the result capped put 512 MiB on a disk whose ceiling was
        # 409.6 MiB.
        for version in ("12", "18"):
            for segment in ("16Mi", "64Mi", "128Mi"):
                for budget in ("1Gi", "4Gi", "64Gi"):
                    with self.subTest(version=version, segment=segment, budget=budget):
                        _, configurator = generate(
                            pg_version=version,
                            replication_mode="physical",
                            wal_segment_size=segment,
                            wal_disk_budget=budget,
                            peak_wal_rate="16Mi",
                            replica_outage_tolerance=3600,
                        )
                        segment_bytes = UnitConverter.size_from(
                            segment, system=UnitConverter.sys_iec
                        )
                        budget_bytes = UnitConverter.size_from(budget, system=UnitConverter.sys_iec)
                        retained = configurator.last_calculation["wal_keep_bytes"]
                        self.assertGreater(retained, 0)
                        self.assertEqual(0, retained % segment_bytes)
                        self.assertLessEqual(retained + segment_bytes, budget_bytes * 0.4)

    def test_the_capped_advisory_reports_the_bytes_that_are_kept(self):
        config, configurator = generate(
            pg_version="12",
            replication_mode="physical",
            wal_segment_size="128Mi",
            wal_disk_budget="1Gi",
            peak_wal_rate="16Mi",
            replica_outage_tolerance=300,
        )
        item = by_code(configurator)["wal_retention_capped"]
        segments = int(config["wal_keep_segments"])

        self.assertEqual("warning", item["severity"])
        self.assertEqual(str(segments), item["actual"])
        self.assertIn(f"{segments} segments", item["message"])
        # What the reader has to compare against the ceiling is the footprint,
        # which is one segment more than the request.
        self.assertIn(
            UnitConverter.size_to((segments + 1) * 128 * MEBIBYTE, system=UnitConverter.sys_pg)
            + " on disk",
            item["message"],
        )

    def test_retention_that_fits_the_budget_is_not_reported_as_capped(self):
        _, configurator = generate(
            pg_version="18",
            replication_mode="physical",
            wal_segment_size="16Mi",
            wal_disk_budget="64Gi",
            peak_wal_rate="1Mi",
            replica_outage_tolerance=60,
        )
        self.assertNotIn("wal_retention_capped", by_code(configurator))


if __name__ == "__main__":
    unittest.main()
