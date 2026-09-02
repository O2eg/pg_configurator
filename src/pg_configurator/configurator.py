import argparse
import copy
import csv
import datetime
import json
import math
import os
import re
import socket
import sys
from enum import Enum

import psutil

from pg_configurator.common import (
    BasicEnum,
    PGConfiguratorResult,
    ResultCode,
    get_default_args,
    get_major_version,
)
from pg_configurator.conf_common import (
    EXTENSION_PRELOAD_ORDER,
    EXTENSION_SPECS,
    MANDATORY_COMMON_EXTENSIONS,
    PROFILE_EXTENSION_DEPENDENCIES,
    common_alg_set,
    common_profile_alg_sets,
)
from pg_configurator.conf_perf import perf_alg_set
from pg_configurator.conf_profiles import (
    alg_set_1c,
    backend_common_alg_set,
    backend_perf_alg_set,
    ext_alg_set,
)
from pg_configurator.orchestration import artifact_hash, capabilities, envelope
from pg_configurator.rule_engine import RuleEvaluationError, RuleEvaluator
from pg_configurator.version import __version__

# What this tool has to say about a configuration comes in three kinds, and
# reporting all of it as "warning" is what makes a reader stop reading. A
# `warning` is a real risk or a conflict in the result. An `assumption` is a
# premise the calculation rests on and could not check. An `info` is a boundary
# of what the tool does, or an explanation of a choice it made.
ADVISORY_SEVERITIES = ("warning", "assumption", "info")


def advisory(code, severity, message, *, setting=None, actual=None):
    """One finding about the configuration that was actually generated."""
    if severity not in ADVISORY_SEVERITIES:
        raise ValueError(f"Unknown advisory severity: {severity}")
    return {
        "code": code,
        "severity": severity,
        "setting": setting,
        "actual": None if actual is None else str(actual),
        "message": message,
    }


def sort_advisories(advisories):
    """Severest first, emission order kept inside each severity."""
    return sorted(advisories, key=lambda item: ADVISORY_SEVERITIES.index(item["severity"]))


class UnitConverter:
    #            kilobytes         megabytes        gigabytes       terabytes
    # PG         kB                MB               GB              TB
    # ISO        K                 M                G               T

    #            kibibytes         mebibytes        gibibytes       tebibytes
    # IEC        Ki                Mi               Gi              Ti

    #            milliseconds    seconds     minutes     hours       days
    # PG         ms              s           min         h           d

    # https://en.wikipedia.org/wiki/Binary_prefix
    # Specific units of IEC 60027-2 A.2 and ISO/IEC 80000

    sys_std = [(1024**4, "T"), (1024**3, "G"), (1024**2, "M"), (1024**1, "K"), (1024**0, "B")]

    sys_iec = [(1024**4, "Ti"), (1024**3, "Gi"), (1024**2, "Mi"), (1024**1, "Ki"), (1024**0, "")]

    sys_iso = [(1000**4, "T"), (1000**3, "G"), (1000**2, "M"), (1000**1, "K"), (1000**0, "B")]

    sys_pg = [
        (1024**4, "TB"),
        (1024**3, "GB"),
        (1024**2, "MB"),
        (1024**1, "kB"),  #   <---------------- PG specific
        (1024**0, ""),
    ]

    @staticmethod
    def size_to(bytes, system=sys_iso, unit=None):
        for factor, postfix in system:
            if (unit is None and bytes / 10 >= factor) or unit == postfix:
                break
        amount = int(bytes / factor)
        return str(amount) + postfix

    @staticmethod
    def size_from(sys_bytes, system=sys_iso):
        if isinstance(sys_bytes, bool):
            raise ValueError("Boolean values are not valid sizes")
        if isinstance(sys_bytes, (int, float)):
            return int(sys_bytes)

        match = re.fullmatch(r"\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*([A-Za-z]*)\s*", str(sys_bytes))
        if match is None:
            raise ValueError(f"Invalid size value: {sys_bytes}")

        amount = float(match.group(1))
        unit = match.group(2)
        for factor, suffix in system:
            if suffix == unit or (suffix == "" and unit == "B"):
                return int(amount * factor)
        raise ValueError(f"Unknown size unit in value: {sys_bytes}")

    @staticmethod
    def size_cpu_to_ncores(cpu_val):
        if isinstance(cpu_val, bool):
            raise ValueError("Boolean values are not valid CPU values")

        match = re.fullmatch(
            r"\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*(m?)\s*", str(cpu_val), flags=re.IGNORECASE
        )
        if match is None:
            raise ValueError(f"Invalid CPU value: {cpu_val}")

        value = float(match.group(1))
        if match.group(2).lower() == "m":
            value /= 1000
        return round(value, 3)


class DutyDB(BasicEnum, Enum):
    STATISTIC = "statistic"  # Analytical queries and large aggregations
    MIXED = "mixed"  # Transactional and analytical queries on one cluster
    OLTP = "oltp"  # High-concurrency short transactions
    FINANCIAL = "financial"  # Latency-sensitive transactions with remote durability option


class DiskType(BasicEnum, Enum):
    # We assume that we have minimum 2 disk in hardware RAID1 (or 4 in RAID10) with BBU
    SATA = "SATA"
    SAS = "SAS"
    SSD = "SSD"
    NVME = "NVME"
    NETWORK = "NETWORK"


class ReplicationMode(BasicEnum, Enum):
    """Replication capability required from the generated cluster."""

    NONE = "none"
    PHYSICAL = "physical"
    LOGICAL = "logical"


class Platform(BasicEnum, Enum):
    WINDOWS = "WINDOWS"
    LINUX = "LINUX"


class OutputFormat(BasicEnum, Enum):
    JSON = "json"
    PATRONI_JSON = "patroni-json"
    CONF = "conf"


def parse_bool(value):
    if isinstance(value, bool):
        return value

    normalized_value = value.strip().lower()
    if normalized_value in ("true", "1", "yes", "on"):
        return True
    if normalized_value in ("false", "0", "no", "off"):
        return False

    raise argparse.ArgumentTypeError("expected one of: true, false, 1, 0, yes, no, on, off")


class PGConfigurator:
    known_versions = {
        "9.6": "settings_pg_9_6.csv",
        "10": "settings_pg_10.csv",
        "11": "settings_pg_11.csv",
        "12": "settings_pg_12.csv",
        "13": "settings_pg_13.csv",
        "14": "settings_pg_14.csv",
        "15": "settings_pg_15.csv",
        "16": "settings_pg_16.csv",
        "17": "settings_pg_17.csv",
        "18": "settings_pg_18.csv",
    }

    conf_profiles = {
        "profile_1c": {
            "alg_set": alg_set_1c,
            "supported_versions": tuple(known_versions),
        },
        "ext_perf": {
            "alg_set": ext_alg_set,
            "supported_versions": tuple(known_versions),
        },
        "profile_backend_common": {
            "alg_set": backend_common_alg_set,
            "supported_versions": tuple(known_versions),
        },
        "profile_backend_perf": {
            "alg_set": backend_perf_alg_set,
            "supported_versions": tuple(known_versions),
        },
    }

    # Compatibility aliases; extension ownership and version metadata live in
    # conf_common.py together with the corresponding rules.
    profile_extensions = PROFILE_EXTENSION_DEPENDENCIES
    extension_supported_versions = {
        name: spec["supported_versions"] for name, spec in EXTENSION_SPECS.items()
    }

    # Used only for old five-column snapshots. Refreshed snapshots carry the
    # authoritative pg_settings.context value and therefore do not rely on
    # this compatibility fallback.
    restart_required_settings = {
        "autovacuum_freeze_max_age",
        "autovacuum_multixact_freeze_max_age",
        "autovacuum_worker_slots",
        "huge_pages",
        "io_max_concurrency",
        "io_method",
        "logging_collector",
        "max_connections",
        "max_files_per_process",
        "max_logical_replication_workers",
        "max_locks_per_transaction",
        "max_pred_locks_per_transaction",
        "max_replication_slots",
        "max_wal_senders",
        "max_worker_processes",
        "shared_buffers",
        "shared_preload_libraries",
        "track_activity_query_size",
        "wal_buffers",
        "wal_level",
        "wal_log_hints",
        "hot_standby",
    }

    # The three main memory budgets are bounded one by one and together.
    # shared_buffers may claim the largest single share: it is one allocation
    # made once at startup, while the other two are multiplied by the number of
    # sessions doing work at the same time.
    #
    # The total stops below the 90% ceiling the computed envelope is held to
    # further down, because that envelope counts what these three parts do not:
    # the lock tables and the per-backend reserve. Allowing the declared parts
    # to reach 90% would let a budget pass this check and then be refused by the
    # envelope with a harder message to act on.
    memory_budget_part_limits = {
        "shared_buffers_part": 0.8,
        "client_mem_part": 0.4,
        "maintenance_mem_part": 0.4,
    }
    memory_budget_total_limit = 0.85

    # End-of-life dates from the PostgreSQL versioning policy
    # (https://www.postgresql.org/support/versioning/), compared against
    # support_horizon rather than the wall clock. artifact_hash deliberately
    # excludes generated_at so that the same inputs hash the same on any day,
    # and an advisory that appeared overnight would break that. A release moves
    # the horizon; because every version is in the table, none can be forgotten.
    postgresql_eol_dates = {
        "9.6": "2021-11-11",
        "10": "2022-11-10",
        "11": "2023-11-09",
        "12": "2024-11-21",
        "13": "2025-11-13",
        "14": "2026-11-12",
        "15": "2027-11-11",
        "16": "2028-11-09",
        "17": "2029-11-08",
        "18": "2030-11-14",
    }
    support_horizon = "2026-09-03"

    current_dir = os.path.dirname(os.path.realpath(__file__))
    output_dir = os.getcwd()
    args = {}
    ext_params = {}

    def __init__(self, args, ext_params):
        self.args = args
        self.ext_params = ext_params
        self.last_artifact = None
        self.last_calculation = {}
        self.last_extensions = []
        self.last_inputs = {}
        self.last_overrides = []
        self.last_parameter_details = {}
        self.last_advisories = []
        if (
            not (
                args.output_file_name.find("""/""") > -1
                or args.output_file_name.find("""\\""") > -1
            )
            and args.output_file_name != ""
        ):
            args.output_file_name = os.path.abspath(
                os.path.join(self.output_dir, args.output_file_name)
            )

    @staticmethod
    def calc_synchronous_commit(duty_db, synchronous_standby_names=""):
        """Choose a truthful durability level.

        ``remote_apply`` is useful only when synchronous standbys are actually
        named. Without them it provides no remote durability and is therefore
        not emitted for the financial profile.
        """

        if duty_db == DutyDB.FINANCIAL and synchronous_standby_names.strip():
            return "remote_apply"
        return "on"

    @staticmethod
    def iterate_alg_set(tune_alg) -> [str, dict]:
        for alg_set_v in sorted(
            [(ver, alg_set_v) for ver, alg_set_v in tune_alg.items()], key=lambda x: float(x[0])
        ):
            yield alg_set_v[0], alg_set_v[1]

    @staticmethod
    def prepare_alg_set(tune_alg, source_name):
        if not isinstance(tune_alg, dict):
            raise ValueError(f"{source_name} must be a versioned rule mapping")
        prepared_tune_alg = copy.deepcopy(tune_alg)

        for ver, version_alg_set in PGConfigurator.iterate_alg_set(tune_alg):
            # inheritance, redefinition, deprecation

            current_ver_deprecated_params = [
                alg["name"]
                for alg in version_alg_set
                if "alg" in alg and alg["alg"] == "deprecated"
            ]

            prepared_tune_alg[ver] = [
                p
                for p in prepared_tune_alg[ver]
                if "name" in p and p["name"] not in current_ver_deprecated_params
            ]

            prepared_tune_alg[ver] = [
                alg
                for alg in version_alg_set
                if not ("alg" in alg and alg["alg"] == "deprecated") and "__parent" not in alg
            ]

            alg_set_current_version = prepared_tune_alg[ver]

            alg_set_from_parent = []
            if len([alg for alg in version_alg_set if "__parent" in alg]) > 0:
                alg_set_from_parent = prepared_tune_alg[
                    [alg for alg in version_alg_set if "__parent" in alg][0]["__parent"]
                ]

            prepared_tune_alg[ver].extend(
                [
                    alg
                    for alg in alg_set_from_parent
                    if "name" in alg
                    and alg["name"] not in [alg["name"] for alg in alg_set_current_version]
                    and alg["name"] not in current_ver_deprecated_params
                ]
            )

        return prepared_tune_alg

    @staticmethod
    def _coerce_enum(value, enum_type, argument_name):
        if isinstance(value, enum_type):
            return value
        try:
            return enum_type(value)
        except (TypeError, ValueError) as error:
            raise ValueError(
                "{} must be one of: {}".format(
                    argument_name, ", ".join(item.value for item in enum_type)
                )
            ) from error

    @staticmethod
    def _validate_fraction_group(values, group_name):
        for name, value in values.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be a number")
            if value <= 0 or value > 1:
                raise ValueError(f"{name} must be greater than 0 and not greater than 1")

        if not math.isclose(sum(values.values()), 1.0, rel_tol=0, abs_tol=1e-9):
            raise ValueError(f"{group_name} must sum to 1.0")

    @classmethod
    def _validate_memory_budget_parts(cls, values):
        for name, value in values.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be a number")
            limit = cls.memory_budget_part_limits[name]
            if value <= 0 or value > limit:
                raise ValueError(f"{name} must be greater than 0 and not greater than {limit}")
        allocated = sum(values.values())
        limit = cls.memory_budget_total_limit
        # Budgets are written as decimals but summed in binary: 0.8 + 0.03 + 0.02
        # is 0.8500000000000001, and refusing that would report arithmetic noise
        # as an overflow. The fraction groups above tolerate the same noise.
        if allocated > limit and not math.isclose(allocated, limit, rel_tol=0, abs_tol=1e-9):
            raise ValueError(
                f"Main memory budgets must use at most {limit:.0%} of available RAM; "
                f"got {allocated:.2%}"
            )

    @staticmethod
    def _validate_positive_range(min_value, max_value, min_name, max_name):
        for name, value in ((min_name, min_value), (max_name, max_value)):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if min_value > max_value:
            raise ValueError(f"{min_name} must not be greater than {max_name}")

    @classmethod
    def _load_setting_metadata(cls, pg_version):
        settings_file = os.path.join(
            cls.current_dir, "pg_settings_history", cls.known_versions[pg_version]
        )
        with open(settings_file, encoding="utf-8") as file_handle:
            reader = csv.DictReader(file_handle)
            return {row["name"]: row for row in reader if row.get("name")}

    @classmethod
    def _validate_config_parameters(
        cls,
        config,
        pg_version,
        *,
        allowed_extension_prefixes=None,
        snapshot_validated_extension_prefixes=None,
    ):
        settings_metadata = cls._load_setting_metadata(pg_version)
        allowed_extension_prefixes = set(allowed_extension_prefixes or ())
        snapshot_validated_extension_prefixes = set(snapshot_validated_extension_prefixes or ())
        unknown_settings = sorted(
            setting_name
            for setting_name in config
            if "." not in setting_name and setting_name not in settings_metadata
        )
        if unknown_settings:
            raise ValueError(
                "Parameters are not supported by PostgreSQL {}: {}".format(
                    pg_version, ", ".join(unknown_settings)
                )
            )

        unknown_extension_settings = sorted(
            setting_name
            for setting_name in config
            if "." in setting_name
            and setting_name.split(".", 1)[0] not in allowed_extension_prefixes
        )
        if unknown_extension_settings:
            raise ValueError(
                "Extension parameters were not declared by an enabled profile or "
                "available_extensions: {}".format(", ".join(unknown_extension_settings))
            )

        missing_snapshot_extension_settings = sorted(
            setting_name
            for setting_name in config
            if "." in setting_name
            and setting_name.split(".", 1)[0] in snapshot_validated_extension_prefixes
            and setting_name not in settings_metadata
        )
        if missing_snapshot_extension_settings:
            raise ValueError(
                "Extension parameters are not present in the PostgreSQL {} snapshot: {}".format(
                    pg_version, ", ".join(missing_snapshot_extension_settings)
                )
            )

        for setting_name, value in config.items():
            metadata = settings_metadata.get(setting_name)
            if metadata is not None:
                cls._validate_setting_value(setting_name, value, metadata, pg_version)

        return settings_metadata

    @staticmethod
    def _parse_pg_array(value):
        value = (value or "").strip()
        if not value or value == "{}":
            return set()
        if value.startswith("{") and value.endswith("}"):
            value = value[1:-1]
        return {item.strip().strip('"') for item in value.split(",") if item.strip()}

    @classmethod
    def _validate_setting_value(cls, setting_name, value, metadata, pg_version):
        """Validate bool/enum/numeric values against a pg_settings snapshot."""

        normalized = str(value).strip().strip("'")
        vartype = metadata.get("vartype", "")
        if not vartype:
            return

        if vartype == "bool":
            if normalized.lower() not in {"on", "off", "true", "false", "yes", "no", "1", "0"}:
                raise ValueError(f"{setting_name} must be a PostgreSQL boolean")
            return

        if vartype == "enum":
            enum_values = cls._parse_pg_array(metadata.get("enumvals", ""))
            if enum_values and normalized not in enum_values:
                raise ValueError(
                    f"{setting_name}={normalized} is not supported by PostgreSQL {pg_version}; "
                    f"expected one of: {', '.join(sorted(enum_values))}"
                )
            return

        if vartype in {"integer", "real"}:
            numeric_match = re.fullmatch(
                r"([+-]?(?:\d+(?:\.\d*)?|\.\d+))([A-Za-z]+|8kB|16MB)?",
                normalized,
            )
            if numeric_match is None:
                raise ValueError(
                    f"{setting_name}={normalized} is not a valid PostgreSQL numeric value"
                )
            setting_value = cls._numeric_value_in_setting_units(
                float(numeric_match.group(1)),
                numeric_match.group(2) or "",
                metadata.get("unit", ""),
            )
            min_value = metadata.get("min_val", "")
            max_value = metadata.get("max_val", "")
            violated_bound = None
            if min_value and setting_value < float(min_value):
                violated_bound = ("min_val", min_value)
            elif max_value and setting_value > float(max_value):
                violated_bound = ("max_val", max_value)
            if violated_bound is not None:
                bound_name, bound = violated_bound
                raise ValueError(
                    f"{setting_name}={normalized} violates PostgreSQL {pg_version} "
                    f"{bound_name}={bound}{metadata.get('unit', '')}"
                )

    @staticmethod
    def _numeric_value_in_setting_units(amount, source_unit, target_unit):
        if not source_unit:
            return amount

        size_factors = {
            "B": 1,
            "kB": 1024,
            "8kB": 8 * 1024,
            "MB": 1024**2,
            "16MB": 16 * 1024**2,
            "GB": 1024**3,
            "TB": 1024**4,
        }
        time_factors = {
            "ms": 0.001,
            "s": 1,
            "min": 60,
            "h": 3600,
            "d": 86400,
        }
        if source_unit in size_factors and target_unit in size_factors:
            return amount * size_factors[source_unit] / size_factors[target_unit]
        if source_unit in time_factors and target_unit in time_factors:
            return amount * time_factors[source_unit] / time_factors[target_unit]
        raise ValueError(
            f"Cannot convert PostgreSQL value from {source_unit} to {target_unit or 'unitless'}"
        )

    @classmethod
    def _setting_context(cls, setting_name, settings_metadata):
        context = settings_metadata.get(setting_name, {}).get("context")
        if context:
            return context, "pg_settings_snapshot"
        if "." in setting_name:
            return "unknown", "external_extension"
        fallback = "postmaster" if setting_name in cls.restart_required_settings else "sighup"
        return fallback, "compatibility_fallback"

    @staticmethod
    def _apply_mode_for_context(context):
        """Map PostgreSQL's GUC context to the minimum deployment action.

        ``reload`` describes applying a value from a configuration source. A
        session-level override can still mask the reloaded default. External
        extension settings without captured ``pg_settings`` metadata require
        target-specific handling and therefore remain ``manual``.
        """

        return {
            "postmaster": "restart",
            "sighup": "reload",
            "backend": "reload_and_reconnect",
            "superuser-backend": "reload_and_reconnect",
            "superuser": "reload",
            "user": "reload",
            "internal": "immutable",
            "unknown": "manual",
        }.get(context, "manual")

    def make_conf(
        self,
        cpu_cores,
        ram_value,
        disk_type=DiskType.SSD,
        duty_db=DutyDB.MIXED,
        replication_enabled=None,
        replication_mode=None,
        pitr_enabled=True,
        synchronous_standby_names="",
        replica_count=1,
        logical_subscription_count=0,
        pg_version="18",
        reserved_ram_percent=10,  # for calc of total_ram_in_bytes
        reserved_system_ram="256Mi",  # for calc of total_ram_in_bytes
        shared_buffers_part=0.25,
        client_mem_part=0.2,  # concurrent query and temporary-memory budget
        maintenance_mem_part=0.1,  # maintenance and autovacuum budget
        autovacuum_workers_mem_part=0.5,  # from maintenance_mem_part
        maintenance_conns_mem_part=0.5,  # from maintenance_mem_part
        min_conns=20,
        max_conns=500,
        min_autovac_workers=3,  # autovacuum workers
        max_autovac_workers=20,
        min_maint_conns=4,  # maintenance connections
        max_maint_conns=16,
        platform=Platform.LINUX,
        common_conf=True,
        conf_profiles=None,
        disk_score=None,
        work_mem_concurrency_factor=4.0,
        peak_wal_rate="4Mi",
        replica_outage_tolerance=900,
        wal_disk_budget="32Gi",
        wal_segment_size="16Mi",
        available_extensions=None,
        db_size=None,
    ):
        # Validate and normalize public inputs before calculating a configuration.
        pg_version = str(pg_version)
        if pg_version not in self.known_versions:
            raise ValueError(f"Unsupported PostgreSQL version: {pg_version}")

        disk_type = self._coerce_enum(disk_type, DiskType, "disk_type")
        duty_db = self._coerce_enum(duty_db, DutyDB, "duty_db")
        platform = self._coerce_enum(platform, Platform, "platform")

        if replication_enabled is not None and not isinstance(replication_enabled, bool):
            raise ValueError("replication_enabled must be a boolean or None")
        if replication_mode is None:
            replication_mode = (
                ReplicationMode.NONE if replication_enabled is False else ReplicationMode.PHYSICAL
            )
        else:
            replication_mode = self._coerce_enum(
                replication_mode, ReplicationMode, "replication_mode"
            )
        if replication_enabled is not None and replication_enabled != (
            replication_mode != ReplicationMode.NONE
        ):
            raise ValueError(
                "replication_enabled conflicts with the explicitly selected replication_mode"
            )
        replication_enabled = replication_mode != ReplicationMode.NONE

        if not isinstance(pitr_enabled, bool):
            raise ValueError("pitr_enabled must be a boolean")
        if not isinstance(synchronous_standby_names, str):
            raise ValueError("synchronous_standby_names must be a string")
        if synchronous_standby_names.strip() and not replication_enabled:
            raise ValueError("synchronous_standby_names requires physical or logical replication")
        if not isinstance(common_conf, bool):
            raise ValueError("common_conf must be a boolean")
        if not common_conf:
            raise ValueError(
                "common_conf cannot be disabled: CSV logging, auto_explain, and "
                "pg_stat_statements are part of the complete configuration contract"
            )
        if conf_profiles is not None and not isinstance(conf_profiles, str):
            raise ValueError("conf_profiles must be a comma-separated string")
        if available_extensions is not None and not isinstance(
            available_extensions, (str, list, tuple, set)
        ):
            raise ValueError("available_extensions must be a comma-separated string or collection")

        selected_profiles = []
        if conf_profiles:
            selected_profiles = [item.strip() for item in conf_profiles.split(",")]
            if any(profile == "" for profile in selected_profiles):
                raise ValueError("Profile name must not be empty")
            if len(selected_profiles) != len(set(selected_profiles)):
                raise ValueError("Profile names must not be repeated")
            unknown_profiles = [
                profile for profile in selected_profiles if profile not in self.conf_profiles
            ]
            if unknown_profiles:
                raise ValueError("Unknown configuration profiles: " + ", ".join(unknown_profiles))
            unsupported_profiles = [
                profile
                for profile in selected_profiles
                if pg_version not in self.conf_profiles[profile]["supported_versions"]
            ]
            if unsupported_profiles:
                raise ValueError(
                    f"Profiles do not support PostgreSQL {pg_version}: "
                    + ", ".join(unsupported_profiles)
                )
            if "profile_1c" in selected_profiles and len(selected_profiles) != 1:
                raise ValueError(
                    "profile_1c is an exclusive compatibility profile and cannot be combined "
                    "with other configuration profiles"
                )

        if disk_score is not None:
            if isinstance(disk_score, bool) or not isinstance(disk_score, (int, float)):
                raise ValueError("disk_score must be a number")
            if disk_score < 0 or disk_score > 100:
                raise ValueError("disk_score must be between 0 and 100")
        if (
            isinstance(work_mem_concurrency_factor, bool)
            or not isinstance(work_mem_concurrency_factor, (int, float))
            or work_mem_concurrency_factor < 1
        ):
            raise ValueError("work_mem_concurrency_factor must be a number not less than 1")

        if isinstance(reserved_ram_percent, bool) or not isinstance(
            reserved_ram_percent, (int, float)
        ):
            raise ValueError("reserved_ram_percent must be a number")
        if reserved_ram_percent < 0 or reserved_ram_percent >= 100:
            raise ValueError(
                "reserved_ram_percent must be greater than or equal to 0 and less than 100"
            )

        self._validate_memory_budget_parts(
            {
                "shared_buffers_part": shared_buffers_part,
                "client_mem_part": client_mem_part,
                "maintenance_mem_part": maintenance_mem_part,
            }
        )
        self._validate_fraction_group(
            {
                "autovacuum_workers_mem_part": autovacuum_workers_mem_part,
                "maintenance_conns_mem_part": maintenance_conns_mem_part,
            },
            "Maintenance memory parts",
        )
        self._validate_positive_range(min_conns, max_conns, "min_conns", "max_conns")
        self._validate_positive_range(
            min_autovac_workers, max_autovac_workers, "min_autovac_workers", "max_autovac_workers"
        )
        if "profile_1c" in selected_profiles and max_autovac_workers < 4:
            raise ValueError("profile_1c requires max_autovac_workers to be at least 4")
        self._validate_positive_range(
            min_maint_conns, max_maint_conns, "min_maint_conns", "max_maint_conns"
        )

        for name, value in {
            "replica_count": replica_count,
            "logical_subscription_count": logical_subscription_count,
        }.items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if pg_version == "9.6" and logical_subscription_count:
            raise ValueError("logical_subscription_count requires PostgreSQL 10 or newer")
        if logical_subscription_count and replication_mode != ReplicationMode.LOGICAL:
            raise ValueError("logical_subscription_count requires replication_mode=logical")
        if (
            isinstance(replica_outage_tolerance, bool)
            or not isinstance(replica_outage_tolerance, int)
            or replica_outage_tolerance < 0
        ):
            raise ValueError("replica_outage_tolerance must be a non-negative integer")

        page_size = 8192
        mebibyte = 1024**2
        gibibyte = 1024**3
        min_work_mem_in_bytes = 1024**2
        default_temp_buffers_in_bytes = 8 * mebibyte
        backend_memory_reserve_in_bytes = 10 * mebibyte

        total_cpu_cores = UnitConverter.size_cpu_to_ncores(cpu_cores)
        if total_cpu_cores <= 0:
            raise ValueError("cpu_cores must be greater than 0")
        cpu_threads = max(1, math.floor(total_cpu_cores))

        ram_in_bytes = UnitConverter.size_from(ram_value, system=UnitConverter.sys_iec)
        reserved_system_ram_in_bytes = UnitConverter.size_from(
            reserved_system_ram, system=UnitConverter.sys_iec
        )
        if ram_in_bytes <= 0:
            raise ValueError("ram_value must be greater than 0")
        if reserved_system_ram_in_bytes < 0:
            raise ValueError("reserved_system_ram must not be negative")

        available_ram_ratio = (100 - reserved_ram_percent) / 100
        total_ram_in_bytes = ram_in_bytes * available_ram_ratio - reserved_system_ram_in_bytes
        if total_ram_in_bytes <= 0:
            raise ValueError(
                "Available RAM must be greater than 0 after reserved memory is subtracted"
            )

        if db_size is not None and (not isinstance(db_size, str) or not db_size.strip()):
            raise ValueError("db_size must be a non-empty IEC size string or None")
        database_size_in_bytes = (
            UnitConverter.size_from(db_size, system=UnitConverter.sys_iec)
            if db_size is not None
            else None
        )
        if database_size_in_bytes is not None and database_size_in_bytes <= 0:
            raise ValueError("db_size must be greater than 0")

        peak_wal_rate_in_bytes = UnitConverter.size_from(
            peak_wal_rate, system=UnitConverter.sys_iec
        )
        wal_disk_budget_in_bytes = UnitConverter.size_from(
            wal_disk_budget, system=UnitConverter.sys_iec
        )
        wal_segment_size_in_bytes = UnitConverter.size_from(
            wal_segment_size, system=UnitConverter.sys_iec
        )
        if peak_wal_rate_in_bytes <= 0:
            raise ValueError("peak_wal_rate must be greater than 0")
        if wal_disk_budget_in_bytes < gibibyte:
            raise ValueError("wal_disk_budget must be at least 1Gi")
        if (
            wal_segment_size_in_bytes < mebibyte
            or wal_segment_size_in_bytes > gibibyte
            or wal_segment_size_in_bytes & (wal_segment_size_in_bytes - 1)
        ):
            raise ValueError("wal_segment_size must be a power-of-two size between 1Mi and 1Gi")
        if wal_disk_budget_in_bytes < wal_segment_size_in_bytes * 8:
            raise ValueError("wal_disk_budget must hold at least eight WAL segments")

        memory_allocated_part = shared_buffers_part + client_mem_part + maintenance_mem_part
        operating_headroom_part = 1.0 - memory_allocated_part
        # Backends may consume only part of the unassigned headroom. Keep the
        # remainder available for lock tables, logical decoding, WAL, contrib
        # workers, kernel buffers, and estimation error.
        connection_overhead_budget = total_ram_in_bytes * operating_headroom_part * 0.67
        connection_capacity = int(connection_overhead_budget / backend_memory_reserve_in_bytes)
        if connection_capacity < min_conns:
            raise ValueError(
                f"Backend memory reserve supports {connection_capacity} connections, "
                f"less than min_conns={min_conns}"
            )

        connections_per_cpu = 5 if duty_db == DutyDB.OLTP else 4
        duty_work_mem_cap_bytes = {
            DutyDB.FINANCIAL: 16 * mebibyte,
            DutyDB.OLTP: 32 * mebibyte,
            DutyDB.MIXED: 64 * mebibyte,
            DutyDB.STATISTIC: 256 * mebibyte,
        }[duty_db]
        autovacuum_cpu_divisor = 6 if duty_db == DutyDB.OLTP else 8
        autovacuum_naptime = {
            DutyDB.FINANCIAL: "30s",
            DutyDB.OLTP: "20s",
            DutyDB.MIXED: "30s",
            DutyDB.STATISTIC: "30s",
        }[duty_db]
        autovacuum_vacuum_scale_factor = {
            DutyDB.FINANCIAL: 0.02,
            DutyDB.OLTP: 0.015,
            DutyDB.MIXED: 0.02,
            DutyDB.STATISTIC: 0.02,
        }[duty_db]
        autovacuum_analyze_scale_factor = {
            DutyDB.FINANCIAL: 0.01,
            DutyDB.OLTP: 0.0075,
            DutyDB.MIXED: 0.01,
            DutyDB.STATISTIC: 0.01,
        }[duty_db]

        def calc_cpu_scale(v_min, v_max):
            if v_min > v_max:
                raise ValueError("v_min must not be greater than v_max")
            cpu_ratio = min(max((cpu_threads - 1) / 95, 0), 1)
            return cpu_ratio * (v_max - v_min) + v_min

        def calc_connection_scale(v_min, v_max):
            if connection_capacity < v_min:
                raise ValueError(
                    f"Client memory budget supports {connection_capacity} connections, "
                    f"less than the required minimum {v_min}"
                )
            cpu_target = v_min + max(0, cpu_threads - 1) * connections_per_cpu
            return int(min(max(cpu_target, v_min), connection_capacity, v_max))

        def calc_connection_limit(desired_connections, required_minimum):
            if desired_connections < required_minimum:
                raise ValueError(
                    f"desired_connections={desired_connections} is less than the required "
                    f"minimum {required_minimum}"
                )
            if connection_capacity < required_minimum:
                raise ValueError(
                    f"Backend memory reserve supports {connection_capacity} connections, "
                    f"less than the required minimum {required_minimum}"
                )
            return int(min(desired_connections, connection_capacity, max_conns))

        def calc_client_mem_values(connection_count, temp_buffers_part=0.1):
            if connection_count <= 0:
                raise ValueError("connection_count must be greater than 0")
            if temp_buffers_part <= 0 or temp_buffers_part >= 1:
                raise ValueError("temp_buffers_part must be greater than 0 and less than 1")

            active_query_sessions = min(connection_count, max(4, cpu_threads * 2))
            client_memory_budget = total_ram_in_bytes * client_mem_part
            temp_buffers_value = min(
                max(
                    default_temp_buffers_in_bytes,
                    client_memory_budget * temp_buffers_part / active_query_sessions,
                ),
                32 * mebibyte,
            )
            work_memory_budget = max(
                client_memory_budget - temp_buffers_value * active_query_sessions,
                min_work_mem_in_bytes
                * active_query_sessions
                * work_mem_concurrency_factor
                * hash_mem_multiplier,
            )
            work_mem_value = work_memory_budget / (
                active_query_sessions * work_mem_concurrency_factor * hash_mem_multiplier
            )
            work_mem_value = min(
                max(work_mem_value, min_work_mem_in_bytes), duty_work_mem_cap_bytes
            )
            return work_mem_value, temp_buffers_value

        maint_max_conns = int(
            min(max(min_maint_conns, math.ceil(cpu_threads / 8)), max_maint_conns)
        )

        default_disk_scores = {
            DiskType.SATA: 15,
            DiskType.SAS: 30,
            DiskType.NETWORK: 45,
            DiskType.SSD: 75,
            DiskType.NVME: 90,
        }
        disk_scores = disk_score if disk_score is not None else default_disk_scores[disk_type]

        def calc_disk_scale(v_min, v_max):
            return (disk_scores / 100) * (v_max - v_min) + v_min

        # Compatibility helper for third-party rules. Bundled rules deliberately
        # do not use a composite score: each setting follows its causal resource.
        cpu_scores = min(cpu_threads / 96 * 100, 100)
        ram_scores = min(total_ram_in_bytes / (768 * gibibyte) * 100, 100)
        system_scores = (cpu_scores + ram_scores + disk_scores) / 3

        def calc_system_scores_scale(v_min, v_max):
            return (system_scores / 100) * (v_max - v_min) + v_min

        max_connections_value = calc_connection_scale(min_conns, max_conns)
        shared_buffers_bytes = total_ram_in_bytes * shared_buffers_part
        maintenance_budget_bytes = total_ram_in_bytes * maintenance_mem_part
        maintenance_work_mem_bytes = min(
            maintenance_budget_bytes * maintenance_conns_mem_part / maint_max_conns,
            2 * gibibyte,
        )

        if "profile_1c" in selected_profiles:
            autovacuum_workers = int(
                min(
                    max(min_autovac_workers, 4, math.ceil(cpu_threads / 4)),
                    max_autovac_workers,
                )
            )
        else:
            autovacuum_workers = int(
                min(
                    max(min_autovac_workers, math.ceil(cpu_threads / autovacuum_cpu_divisor) + 2),
                    max_autovac_workers,
                )
            )
        autovacuum_work_mem_bytes = min(
            maintenance_budget_bytes * autovacuum_workers_mem_part / autovacuum_workers,
            1024 * mebibyte,
        )

        if cpu_threads < 2 or total_ram_in_bytes < 2 * gibibyte:
            parallel_worker_budget = 0
        else:
            cpu_parallel_worker_budget = max(
                1,
                int(
                    cpu_threads
                    * {
                        DutyDB.FINANCIAL: 0.25,
                        DutyDB.OLTP: 0.35,
                        DutyDB.MIXED: 0.5,
                        DutyDB.STATISTIC: 0.75,
                    }[duty_db]
                ),
            )
            ram_parallel_worker_budget = max(1, int(total_ram_in_bytes / (2 * gibibyte)))
            parallel_worker_budget = min(
                32,
                cpu_parallel_worker_budget,
                ram_parallel_worker_budget,
            )
        logical_worker_budget = (
            min(16, max(2, logical_subscription_count * 2 + 2))
            if replication_mode == ReplicationMode.LOGICAL
            else 0
        )
        extension_worker_reserve = 4 if common_conf or conf_profiles else 2
        worker_process_budget = min(
            64,
            max(
                8,
                parallel_worker_budget + logical_worker_budget + extension_worker_reserve + 2,
            ),
        )
        parallel_workers_per_gather = min(
            parallel_worker_budget,
            {
                DutyDB.FINANCIAL: 2,
                DutyDB.OLTP: 2,
                DutyDB.MIXED: 4,
                DutyDB.STATISTIC: 8,
            }[duty_db],
        )
        parallel_maintenance_workers = min(
            parallel_worker_budget,
            {
                DutyDB.FINANCIAL: 2,
                DutyDB.OLTP: 2,
                DutyDB.MIXED: 4,
                DutyDB.STATISTIC: 8,
            }[duty_db],
        )
        parallel_setup_cost = {
            DutyDB.FINANCIAL: 2000,
            DutyDB.OLTP: 1500,
            DutyDB.MIXED: 1000,
            DutyDB.STATISTIC: 500,
        }[duty_db]
        parallel_tuple_cost = {
            DutyDB.FINANCIAL: 0.15,
            DutyDB.OLTP: 0.12,
            DutyDB.MIXED: 0.10,
            DutyDB.STATISTIC: 0.05,
        }[duty_db]
        min_parallel_table_scan_size = {
            DutyDB.FINANCIAL: "32MB",
            DutyDB.OLTP: "16MB",
            DutyDB.MIXED: "8MB",
            DutyDB.STATISTIC: "4MB",
        }[duty_db]
        min_parallel_index_scan_size = {
            DutyDB.FINANCIAL: "2MB",
            DutyDB.OLTP: "1MB",
            DutyDB.MIXED: "512kB",
            DutyDB.STATISTIC: "256kB",
        }[duty_db]
        sync_workers_per_subscription = (
            min(4, max(1, logical_worker_budget // 2)) if logical_worker_budget else 0
        )
        parallel_apply_workers = min(4, max(0, logical_worker_budget - 2))

        effective_replica_count = replica_count if replication_enabled else 0
        effective_logical_subscriptions = (
            logical_subscription_count if replication_mode == ReplicationMode.LOGICAL else 0
        )
        replication_slot_budget = (
            effective_replica_count + effective_logical_subscriptions + 2
            if replication_enabled
            else 0
        )
        wal_sender_budget = (
            replication_slot_budget + effective_replica_count + 2 if replication_enabled else 0
        )
        wal_level = (
            "logical"
            if replication_mode == ReplicationMode.LOGICAL
            else "replica"
            if replication_enabled or pitr_enabled
            else "minimal"
        )
        synchronous_commit = self.calc_synchronous_commit(duty_db, synchronous_standby_names)
        max_standby_streaming_delay = {
            DutyDB.FINANCIAL: "30s",
            DutyDB.OLTP: "45s",
            DutyDB.MIXED: "60s",
            DutyDB.STATISTIC: "5min",
        }[duty_db]

        checkpoint_timeout_seconds = {
            DutyDB.FINANCIAL: 300,
            DutyDB.OLTP: 600,
            DutyDB.MIXED: 900,
            DutyDB.STATISTIC: 1800,
        }[duty_db]
        checkpoint_timeout = f"{checkpoint_timeout_seconds // 60}min"
        max_wal_size_bytes = min(
            max(
                peak_wal_rate_in_bytes * checkpoint_timeout_seconds * 2,
                gibibyte,
                wal_segment_size_in_bytes * 4,
            ),
            wal_disk_budget_in_bytes * 0.5,
        )
        min_wal_size_bytes = min(
            max(max_wal_size_bytes / 4, wal_segment_size_in_bytes * 2),
            4 * gibibyte,
        )
        # Retention is decided in whole segments and only then in bytes, because
        # that is how PostgreSQL spends the disk: a byte request is converted
        # down to segments, and the segment currently being written is held on
        # top of whatever was asked for. Rounding the request up and calling the
        # result capped at 40% put 512 MiB on a disk whose ceiling was 409.6.
        # wal_disk_budget is checked above to hold at least eight segments, so
        # the ceiling below is never under three and the retained count never
        # under one.
        desired_wal_keep_bytes = peak_wal_rate_in_bytes * replica_outage_tolerance
        wal_keep_budget_bytes = wal_disk_budget_in_bytes * 0.4
        wal_keep_segments = (
            max(
                1,
                min(
                    math.ceil(
                        max(desired_wal_keep_bytes, 512 * mebibyte) / wal_segment_size_in_bytes
                    ),
                    int(wal_keep_budget_bytes // wal_segment_size_in_bytes) - 1,
                ),
            )
            if replication_enabled
            else 0
        )
        wal_keep_bytes = wal_keep_segments * wal_segment_size_in_bytes
        max_slot_wal_keep_size_bytes = (
            wal_disk_budget_in_bytes * 0.4 if replication_slot_budget else 0
        )

        effective_io_concurrency = (
            2
            if disk_scores < 25
            else 16
            if disk_scores < 50
            else 64
            if disk_scores < 75
            else 128
            if disk_scores < 90
            else 256
        )
        maintenance_io_concurrency = max(2, min(64, effective_io_concurrency // 2))
        random_page_cost = round(4.0 - (disk_scores / 100) * 2.9, 2)

        if platform == Platform.WINDOWS:
            io_combine_limit_bytes = 128 * 1024
        else:
            io_combine_limit_bytes = {
                DutyDB.FINANCIAL: 128 * 1024,
                DutyDB.OLTP: 256 * 1024 if disk_scores >= 50 else 128 * 1024,
                DutyDB.MIXED: 512 * 1024 if disk_scores >= 75 else 256 * 1024,
                DutyDB.STATISTIC: 1024 * 1024 if disk_scores >= 75 else 512 * 1024,
            }[duty_db]
        if get_major_version(pg_version) == 17:
            io_combine_limit_bytes = min(io_combine_limit_bytes, 256 * 1024)
        io_max_combine_limit_bytes = io_combine_limit_bytes

        vacuum_buffer_usage_limit_bytes = min(
            shared_buffers_bytes / 8,
            {
                DutyDB.FINANCIAL: 1024 * 1024,
                DutyDB.OLTP: 2 * mebibyte,
                DutyDB.MIXED: 8 * mebibyte,
                DutyDB.STATISTIC: 32 * mebibyte,
            }[duty_db],
        )
        if "profile_1c" in selected_profiles:
            vacuum_buffer_usage_limit_bytes = min(
                vacuum_buffer_usage_limit_bytes,
                2 * mebibyte,
            )

        statistics_tiers = (100, 500, 1000, 2500, 5000)

        def round_statistics_target(value):
            return next((tier for tier in statistics_tiers if tier >= value), statistics_tiers[-1])

        statistics_cpu_cap = (
            500
            if cpu_threads < 4
            else 1000
            if cpu_threads < 8
            else 2500
            if cpu_threads < 16
            else 5000
        )
        statistics_ram_cap = (
            500
            if total_ram_in_bytes < 8 * gibibyte
            else 1000
            if total_ram_in_bytes < 32 * gibibyte
            else 2500
            if total_ram_in_bytes < 128 * gibibyte
            else 5000
        )
        statistics_resource_cap = min(statistics_cpu_cap, statistics_ram_cap)
        statistics_size_multiplier = (
            1
            if database_size_in_bytes is None
            else 1
            if database_size_in_bytes < 100 * gibibyte
            else 2
            if database_size_in_bytes < 1024 * gibibyte
            else 4
        )
        statistics_duty_base = {
            DutyDB.FINANCIAL: 500,
            DutyDB.OLTP: 500,
            DutyDB.MIXED: 1000,
            DutyDB.STATISTIC: 2500,
        }[duty_db]
        statistics_duty_cap = {
            DutyDB.FINANCIAL: 1000,
            DutyDB.OLTP: 2500,
            DutyDB.MIXED: 5000,
            DutyDB.STATISTIC: 5000,
        }[duty_db]
        default_statistics_target = min(
            statistics_resource_cap,
            statistics_duty_cap,
            round_statistics_target(statistics_duty_base * statistics_size_multiplier),
        )
        profile_1c_statistics_target = min(
            statistics_resource_cap,
            round_statistics_target(1000 * statistics_size_multiplier),
        )
        profile_backend_statistics_target = min(
            statistics_resource_cap,
            max(500, default_statistics_target),
        )

        jit = "on" if duty_db in {DutyDB.MIXED, DutyDB.STATISTIC} else "off"
        jit_above_cost = 50000 if duty_db == DutyDB.STATISTIC else 100000
        jit_inline_above_cost = 250000 if duty_db == DutyDB.STATISTIC else 500000
        jit_optimize_above_cost = 250000 if duty_db == DutyDB.STATISTIC else 500000
        autovacuum_cost_limit = int(500 + disk_scores * 20)
        autovacuum_cost_delay_ms = 10 if disk_scores < 25 else 5 if disk_scores < 60 else 2
        available_ram_gib = total_ram_in_bytes / gibibyte
        lock_tier = (
            64
            if available_ram_gib < 4
            else 128
            if available_ram_gib < 16
            else 256
            if available_ram_gib < 64
            else 512
            if available_ram_gib < 256
            else 1024
        )
        connection_lock_target = max_connections_value
        if "profile_1c" in selected_profiles:
            connection_lock_target = calc_connection_limit(1000, min_conns)
        max_locks_per_transaction = min(
            2048,
            max(lock_tier, 64 + math.ceil(connection_lock_target / 4)),
        )
        if "profile_1c" in selected_profiles:
            max_locks_per_transaction = min(2000, max(512, max_locks_per_transaction))
        max_pred_locks_per_transaction = min(
            1024,
            max(64, max_locks_per_transaction // 2),
        )
        max_pred_locks_per_page = min(
            16,
            max(2, max_pred_locks_per_transaction // 64),
        )
        max_pred_locks_per_relation = min(
            max_pred_locks_per_transaction,
            max(64, max_pred_locks_per_transaction // 2),
        )
        hash_mem_multiplier = 1.5 if duty_db in {DutyDB.FINANCIAL, DutyDB.OLTP} else 2.0
        logical_connection_budget = max(1, replication_slot_budget)
        logical_decoding_work_mem_bytes = min(
            256 * mebibyte,
            max(
                64 * mebibyte,
                total_ram_in_bytes * client_mem_part * 0.25 / logical_connection_budget,
            ),
        )
        estimated_lock_process_count = (
            connection_lock_target + worker_process_budget + autovacuum_workers + wal_sender_budget
        )
        estimated_lock_memory_bytes = (
            max_locks_per_transaction * estimated_lock_process_count * 270
            + max_pred_locks_per_transaction * connection_lock_target * 64
        )
        estimated_logical_decoding_memory_bytes = (
            logical_decoding_work_mem_bytes * max(1, effective_logical_subscriptions)
            if replication_mode == ReplicationMode.LOGICAL and get_major_version(pg_version) >= 13
            else 0
        )
        effective_cache_size_bytes = max(
            shared_buffers_bytes,
            total_ram_in_bytes
            - total_ram_in_bytes * client_mem_part
            - maintenance_budget_bytes
            - connection_lock_target * backend_memory_reserve_in_bytes
            - estimated_lock_memory_bytes
            - estimated_logical_decoding_memory_bytes,
        )
        autovacuum_worker_slots = min(
            max_autovac_workers,
            max(autovacuum_workers, autovacuum_workers + 2),
        )
        io_workers = min(8, max(3, math.ceil(cpu_threads / 8)))
        lock_timeout = {
            DutyDB.FINANCIAL: "5s",
            DutyDB.OLTP: "10s",
            DutyDB.MIXED: "15s",
            DutyDB.STATISTIC: "1min",
        }[duty_db]
        statement_timeout = {
            DutyDB.FINANCIAL: "5min",
            DutyDB.OLTP: "15min",
            DutyDB.MIXED: "30min",
            DutyDB.STATISTIC: "4h",
        }[duty_db]
        idle_in_transaction_session_timeout = {
            DutyDB.FINANCIAL: "5min",
            DutyDB.OLTP: "10min",
            DutyDB.MIXED: "15min",
            DutyDB.STATISTIC: "1h",
        }[duty_db]
        idle_session_timeout = {
            DutyDB.FINANCIAL: "4h",
            DutyDB.OLTP: "6h",
            DutyDB.MIXED: "8h",
            DutyDB.STATISTIC: "24h",
        }[duty_db]
        transaction_timeout = {
            DutyDB.FINANCIAL: "30min",
            DutyDB.OLTP: "1h",
            DutyDB.MIXED: "2h",
            DutyDB.STATISTIC: "8h",
        }[duty_db]
        tcp_keepalives_idle_seconds = {
            DutyDB.FINANCIAL: 60,
            DutyDB.OLTP: 90,
            DutyDB.MIXED: 120,
            DutyDB.STATISTIC: 300,
        }[duty_db]
        tcp_keepalives_interval_seconds = {
            DutyDB.FINANCIAL: 10,
            DutyDB.OLTP: 15,
            DutyDB.MIXED: 30,
            DutyDB.STATISTIC: 30,
        }[duty_db]
        desired_tcp_keepalives_count = {
            DutyDB.FINANCIAL: 6,
            DutyDB.OLTP: 6,
            DutyDB.MIXED: 4,
            DutyDB.STATISTIC: 3,
        }[duty_db]
        tcp_keepalives_count = desired_tcp_keepalives_count if platform == Platform.LINUX else 0
        network_failure_detection_seconds = (
            tcp_keepalives_idle_seconds + tcp_keepalives_interval_seconds * tcp_keepalives_count
            if tcp_keepalives_count
            else None
        )
        tcp_user_timeout = (
            f"{network_failure_detection_seconds}s" if platform == Platform.LINUX else "0"
        )
        client_connection_check_interval = (
            {
                DutyDB.FINANCIAL: "5s",
                DutyDB.OLTP: "10s",
                DutyDB.MIXED: "10s",
                DutyDB.STATISTIC: "30s",
            }[duty_db]
            if platform == Platform.LINUX
            else "0"
        )
        replication_network_timeout = {
            DutyDB.FINANCIAL: "60s",
            DutyDB.OLTP: "90s",
            DutyDB.MIXED: "120s",
            DutyDB.STATISTIC: "300s",
        }[duty_db]
        authentication_timeout = "30s"
        deadlock_timeout = "2s" if duty_db == DutyDB.STATISTIC else "1s"

        required_extensions = set(MANDATORY_COMMON_EXTENSIONS)
        for profile in selected_profiles:
            required_extensions.update(self.profile_extensions.get(profile, set()))

        unsupported_extensions = sorted(
            extension
            for extension in required_extensions
            if pg_version not in self.extension_supported_versions.get(extension, ())
        )
        if unsupported_extensions:
            raise ValueError(
                f"Extensions have no bundled PostgreSQL {pg_version} rules: "
                + ", ".join(unsupported_extensions)
            )

        normalized_available_extensions = None
        if available_extensions is not None:
            if isinstance(available_extensions, str):
                normalized_available_extensions = {
                    item.strip() for item in available_extensions.split(",") if item.strip()
                }
            else:
                normalized_available_extensions = {
                    str(item).strip() for item in available_extensions if str(item).strip()
                }

        if required_extensions and normalized_available_extensions is not None:
            missing_extensions = sorted(required_extensions - normalized_available_extensions)
            if missing_extensions:
                raise ValueError(
                    "Required extensions are unavailable: {}".format(", ".join(missing_extensions))
                )

        shared_preload_libraries_value = ",".join(
            extension for extension in EXTENSION_PRELOAD_ORDER if extension in required_extensions
        )
        auto_explain_log_min_duration = {
            DutyDB.FINANCIAL: "5s",
            DutyDB.OLTP: "8s",
            DutyDB.MIXED: "10s",
            DutyDB.STATISTIC: "30s",
        }[duty_db]
        auto_explain_sample_rate = {
            DutyDB.FINANCIAL: 0.01,
            DutyDB.OLTP: 0.015,
            DutyDB.MIXED: 0.02,
            DutyDB.STATISTIC: 0.05,
        }[duty_db]
        log_transaction_sample_rate = {
            DutyDB.FINANCIAL: 0.0001,
            DutyDB.OLTP: 0.00025,
            DutyDB.MIXED: 0.0005,
            DutyDB.STATISTIC: 0.001,
        }[duty_db]
        log_statement_sample_rate = {
            DutyDB.FINANCIAL: 0.001,
            DutyDB.OLTP: 0.0025,
            DutyDB.MIXED: 0.005,
            DutyDB.STATISTIC: 0.01,
        }[duty_db]
        log_min_duration_statement = auto_explain_log_min_duration

        # Apply profiles to a per-run copy. Bundled rules must remain immutable
        # because pg_play can calculate several candidate configurations in one process.
        effective_perf_alg_set = copy.deepcopy(perf_alg_set)
        for rules in effective_perf_alg_set.values():
            for rule in rules:
                rule.setdefault("_source", "base")

        if selected_profiles:
            for profile in selected_profiles:
                if profile not in self.conf_profiles:
                    raise ValueError(f"Profile {profile} not found! See directory 'conf_profiles'")

                profile_spec = self.conf_profiles[profile]
                if pg_version not in profile_spec["supported_versions"]:
                    raise ValueError(f"Profile {profile} does not support PostgreSQL {pg_version}")

                profile_alg_set = profile_spec["alg_set"]
                for version, profile_rules in profile_alg_set.items():
                    if version not in effective_perf_alg_set:
                        raise ValueError(
                            f"Profile {profile} contains unsupported PostgreSQL version {version}"
                        )
                    tagged_profile_rules = copy.deepcopy(profile_rules)
                    for rule in tagged_profile_rules:
                        rule["_source"] = profile
                    effective_perf_alg_set[version].extend(tagged_profile_rules)

        d1 = {}
        # Merge params in versions to avoid duplicates
        for ver, _params in effective_perf_alg_set.items():
            d1[ver] = {
                d["name"]: {k: v for k, v in d.items() if k != "name"}
                for d in effective_perf_alg_set[ver]
                if "name" in d
            }

        d2 = {}
        for ver, version_rules in effective_perf_alg_set.items():
            parent = next(
                (rule["__parent"] for rule in version_rules if "__parent" in rule),
                None,
            )
            d2[ver] = {"__parent": parent} if parent is not None else {}

        perf_alg_set_res = {}
        for ver, param in d2.items():
            perf_alg_set_res[ver] = [{k: v for k, v in param.items() if isinstance(v, str)}]

        for ver, param in d1.items():
            perf_alg_set_res[ver].extend(
                [{**{"name": k}, **v} for k, v in param.items() if isinstance(v, dict)]
            )

        prepared_alg_set = PGConfigurator.prepare_alg_set(perf_alg_set_res, "conf_perf")[pg_version]

        if common_conf:
            prepared_common_alg_set = PGConfigurator.prepare_alg_set(common_alg_set, "conf_common")[
                pg_version
            ]
            for rule in prepared_common_alg_set:
                rule["_source"] = "common"
            prepared_alg_set.extend(prepared_common_alg_set)
            for profile in selected_profiles:
                profile_common_rules = common_profile_alg_sets.get(profile)
                if profile_common_rules is None:
                    continue
                prepared_profile_common_rules = PGConfigurator.prepare_alg_set(
                    profile_common_rules,
                    f"conf_common:{profile}",
                )[pg_version]
                for rule in prepared_profile_common_rules:
                    rule["_source"] = f"common:{profile}"
                prepared_alg_set.extend(prepared_profile_common_rules)

        if (
            self.ext_params is not None and len(self.ext_params) > 0
        ):  # ext_params initialized in unit tests
            external_rules = copy.deepcopy(self.ext_params)
            for rule in external_rules:
                rule["_source"] = "external"
            prepared_alg_set.extend(external_rules)

        base_parameter_names = {
            rule["name"]
            for rule in PGConfigurator.prepare_alg_set(perf_alg_set, "conf_perf_base")[pg_version]
            if "name" in rule
        }
        rule_context = {
            "DiskType": DiskType,
            "DutyDB": DutyDB,
            "PGConfigurator": PGConfigurator,
            "Platform": Platform,
            "ReplicationMode": ReplicationMode,
            "UnitConverter": UnitConverter,
            "autovacuum_cost_delay_ms": autovacuum_cost_delay_ms,
            "autovacuum_cost_limit": autovacuum_cost_limit,
            "autovacuum_naptime": autovacuum_naptime,
            "autovacuum_analyze_scale_factor": autovacuum_analyze_scale_factor,
            "autovacuum_vacuum_scale_factor": autovacuum_vacuum_scale_factor,
            "autovacuum_worker_slots": autovacuum_worker_slots,
            "autovacuum_workers": autovacuum_workers,
            "autovacuum_work_mem_bytes": autovacuum_work_mem_bytes,
            "autovacuum_workers_mem_part": autovacuum_workers_mem_part,
            "authentication_timeout": authentication_timeout,
            "calc_client_mem_values": calc_client_mem_values,
            "calc_connection_limit": calc_connection_limit,
            "calc_connection_scale": calc_connection_scale,
            "calc_cpu_scale": calc_cpu_scale,
            "calc_disk_scale": calc_disk_scale,
            "calc_system_scores_scale": calc_system_scores_scale,
            "client_connection_check_interval": client_connection_check_interval,
            "deadlock_timeout": deadlock_timeout,
            "default_statistics_target": default_statistics_target,
            "disk_scores": disk_scores,
            "disk_type": disk_type,
            "duty_db": duty_db,
            "effective_cache_size_bytes": effective_cache_size_bytes,
            "effective_io_concurrency": effective_io_concurrency,
            "float": float,
            "hash_mem_multiplier": hash_mem_multiplier,
            "idle_in_transaction_session_timeout": idle_in_transaction_session_timeout,
            "idle_session_timeout": idle_session_timeout,
            "int": int,
            "io_combine_limit_bytes": io_combine_limit_bytes,
            "io_max_combine_limit_bytes": io_max_combine_limit_bytes,
            "io_workers": io_workers,
            "jit": jit,
            "jit_above_cost": jit_above_cost,
            "jit_inline_above_cost": jit_inline_above_cost,
            "jit_optimize_above_cost": jit_optimize_above_cost,
            "logical_decoding_work_mem_bytes": logical_decoding_work_mem_bytes,
            "logical_worker_budget": logical_worker_budget,
            "lock_timeout": lock_timeout,
            "log_min_duration_statement": log_min_duration_statement,
            "maint_max_conns": maint_max_conns,
            "maintenance_io_concurrency": maintenance_io_concurrency,
            "maintenance_conns_mem_part": maintenance_conns_mem_part,
            "maintenance_mem_part": maintenance_mem_part,
            "maintenance_work_mem_bytes": maintenance_work_mem_bytes,
            "max": max,
            "max_autovac_workers": max_autovac_workers,
            "max_connections": max_connections_value,
            "max_connections_value": max_connections_value,
            "max_conns": max_conns,
            "max_locks_per_transaction": max_locks_per_transaction,
            "max_pred_locks_per_page": max_pred_locks_per_page,
            "max_pred_locks_per_relation": max_pred_locks_per_relation,
            "max_pred_locks_per_transaction": max_pred_locks_per_transaction,
            "max_slot_wal_keep_size_bytes": max_slot_wal_keep_size_bytes,
            "max_standby_streaming_delay": max_standby_streaming_delay,
            "max_wal_size_bytes": max_wal_size_bytes,
            "min": min,
            "min_autovac_workers": min_autovac_workers,
            "min_conns": min_conns,
            "min_parallel_index_scan_size": min_parallel_index_scan_size,
            "min_parallel_table_scan_size": min_parallel_table_scan_size,
            "min_wal_size_bytes": min_wal_size_bytes,
            "page_size": page_size,
            "parallel_apply_workers": parallel_apply_workers,
            "parallel_maintenance_workers": parallel_maintenance_workers,
            "parallel_setup_cost": parallel_setup_cost,
            "parallel_tuple_cost": parallel_tuple_cost,
            "parallel_worker_budget": parallel_worker_budget,
            "parallel_workers_per_gather": parallel_workers_per_gather,
            "pitr_enabled": pitr_enabled,
            "platform": platform,
            "random_page_cost": random_page_cost,
            "replication_mode": replication_mode,
            "replication_enabled": replication_enabled,
            "replication_network_timeout": replication_network_timeout,
            "replication_slot_budget": replication_slot_budget,
            "round": round,
            "shared_buffers": shared_buffers_bytes,
            "shared_buffers_bytes": shared_buffers_bytes,
            "shared_buffers_part": shared_buffers_part,
            "shared_preload_libraries_value": shared_preload_libraries_value,
            "profile_1c_statistics_target": profile_1c_statistics_target,
            "profile_backend_statistics_target": profile_backend_statistics_target,
            "sync_workers_per_subscription": sync_workers_per_subscription,
            "statement_timeout": statement_timeout,
            "synchronous_commit": synchronous_commit,
            "synchronous_standby_names": synchronous_standby_names,
            "total_cpu_cores": total_cpu_cores,
            "total_ram_in_bytes": total_ram_in_bytes,
            "wal_keep_bytes": wal_keep_bytes,
            "wal_keep_segments": wal_keep_segments,
            "wal_level": wal_level,
            "wal_sender_budget": wal_sender_budget,
            "worker_process_budget": worker_process_budget,
            "transaction_timeout": transaction_timeout,
            "tcp_keepalives_count": tcp_keepalives_count,
            "tcp_keepalives_idle_seconds": tcp_keepalives_idle_seconds,
            "tcp_keepalives_interval_seconds": tcp_keepalives_interval_seconds,
            "tcp_user_timeout": tcp_user_timeout,
            "checkpoint_timeout": checkpoint_timeout,
            "connection_lock_target": connection_lock_target,
            "auto_explain_log_min_duration": auto_explain_log_min_duration,
            "auto_explain_sample_rate": auto_explain_sample_rate,
            "log_statement_sample_rate": log_statement_sample_rate,
            "log_transaction_sample_rate": log_transaction_sample_rate,
            "vacuum_buffer_usage_limit_bytes": vacuum_buffer_usage_limit_bytes,
        }
        rule_evaluator = RuleEvaluator(
            rule_context,
            allowed_callables={
                PGConfigurator.calc_synchronous_commit,
                UnitConverter.size_from,
                calc_client_mem_values,
                calc_connection_limit,
                calc_connection_scale,
                calc_cpu_scale,
                calc_disk_scale,
                calc_system_scores_scale,
                float,
                int,
                max,
                min,
                round,
            },
            allowed_attribute_roots={
                DiskType,
                DutyDB,
                PGConfigurator,
                Platform,
                ReplicationMode,
                UnitConverter,
            },
        )

        settings_metadata = self._load_setting_metadata(pg_version)
        config_res = {}
        parameter_details = {}
        overrides = []
        for param in prepared_alg_set:
            if "name" not in param:
                continue
            param_name = param["name"]
            rule_expression = param["alg"].strip() if "alg" in param else None
            source = param.get("_source", "base")

            debug_enabled = getattr(
                self.args,
                "debug",
                getattr(self.args, "debug_mode", False),
            )

            try:
                raw_value = (
                    param["const"] if "const" in param else rule_evaluator.evaluate(rule_expression)
                )
            except (RuleEvaluationError, TypeError, ValueError, ZeroDivisionError) as error:
                raise RuleEvaluationError(
                    f"Failed to calculate {param_name} from source {source}: {error}"
                ) from error

            if "unit_postfix" in param:
                formatted_value = str(raw_value) + param["unit_postfix"]
            elif param.get("to_unit") == "as_is":
                formatted_value = str(raw_value)
            elif param.get("to_unit") == "quote":
                formatted_value = f"'{raw_value}'"
            elif "alg" in param:
                formatted_value = UnitConverter.size_to(
                    raw_value, system=UnitConverter.sys_pg, unit=param.get("to_unit")
                )
            else:
                formatted_value = str(raw_value)

            if param_name in parameter_details:
                overrides.append(
                    {
                        "parameter": param_name,
                        "from": parameter_details[param_name]["source"],
                        "to": source,
                    }
                )
            elif source != "base" and param_name in base_parameter_names:
                overrides.append({"parameter": param_name, "from": "base", "to": source})

            config_res[param_name] = formatted_value
            setting_context, context_source = self._setting_context(param_name, settings_metadata)
            parameter_details[param_name] = {
                "value": formatted_value,
                "raw_value": raw_value,
                "source": source,
                "rule": rule_expression,
                "rule_kind": "expression" if rule_expression is not None else "constant",
                "context": setting_context,
                "context_source": context_source,
                "apply_mode": self._apply_mode_for_context(setting_context),
            }
            if debug_enabled:
                print(
                    "# rule {}: source={}, kind={}, expression={}, raw={!r}, value={}".format(
                        param_name,
                        source,
                        parameter_details[param_name]["rule_kind"],
                        rule_expression,
                        raw_value,
                        formatted_value,
                    ),
                    file=sys.stderr,
                )
            if param_name.isidentifier():
                rule_context[param_name] = raw_value

        config_res = dict(sorted(config_res.items()))
        allowed_extension_prefixes = required_extensions | set(
            normalized_available_extensions or ()
        )
        self._validate_config_parameters(
            config_res,
            pg_version,
            allowed_extension_prefixes=allowed_extension_prefixes,
            snapshot_validated_extension_prefixes={
                name
                for name in required_extensions
                if EXTENSION_SPECS[name]["settings_validation"] == "pg_settings_snapshot"
            },
        )

        if config_res.get("full_page_writes") != "on" or config_res.get("fsync") != "on":
            raise ValueError("Bundled safety invariant requires fsync=on and full_page_writes=on")
        if config_res.get("synchronous_commit") == "remote_apply" and not (
            synchronous_standby_names.strip()
        ):
            raise ValueError("synchronous_commit=remote_apply requires synchronous_standby_names")
        if config_res.get("synchronous_commit") not in {"on", "remote_apply"}:
            raise ValueError(
                "Bundled safety invariant requires synchronous_commit=on or remote_apply"
            )
        if config_res.get("wal_level") == "minimal" and (pitr_enabled or replication_enabled):
            raise ValueError("wal_level=minimal conflicts with PITR or replication")
        if config_res.get("logging_collector") != "on" or "csvlog" not in config_res.get(
            "log_destination", ""
        ):
            raise ValueError("Bundled observability invariant requires CSV logging collector")
        preloaded_libraries = {
            library.strip()
            for library in config_res.get("shared_preload_libraries", "").strip("'").split(",")
            if library.strip()
        }
        if not MANDATORY_COMMON_EXTENSIONS.issubset(preloaded_libraries):
            raise ValueError(
                "Bundled observability invariant requires auto_explain and pg_stat_statements"
            )

        max_worker_processes = int(config_res["max_worker_processes"])
        max_parallel_workers = int(
            config_res.get("max_parallel_workers", config_res["max_parallel_workers_per_gather"])
        )
        max_logical_workers = int(config_res.get("max_logical_replication_workers", 0))
        required_worker_processes = (
            max_parallel_workers + max_logical_workers + extension_worker_reserve + 2
        )
        if max_worker_processes < required_worker_processes:
            raise ValueError(
                "max_worker_processes must reserve capacity for parallel, logical, "
                "extension, and maintenance workers"
            )
        if int(config_res.get("max_sync_workers_per_subscription", 0)) > max_logical_workers:
            raise ValueError("max_sync_workers_per_subscription exceeds logical worker capacity")
        if (
            int(config_res.get("max_parallel_apply_workers_per_subscription", 0))
            > max_logical_workers
        ):
            raise ValueError(
                "max_parallel_apply_workers_per_subscription exceeds logical worker capacity"
            )
        if int(config_res["max_parallel_workers_per_gather"]) > max_worker_processes:
            raise ValueError("max_parallel_workers_per_gather exceeds worker-process capacity")
        if int(config_res.get("max_parallel_maintenance_workers", 0)) > max_parallel_workers:
            raise ValueError("max_parallel_maintenance_workers exceeds parallel worker capacity")

        reserved_connection_total = int(config_res["superuser_reserved_connections"]) + int(
            config_res.get("reserved_connections", 0)
        )
        if reserved_connection_total >= int(config_res["max_connections"]):
            raise ValueError(
                "superuser_reserved_connections plus reserved_connections must be below "
                "max_connections"
            )
        if UnitConverter.size_from(
            config_res["effective_cache_size"], system=UnitConverter.sys_pg
        ) < UnitConverter.size_from(config_res["shared_buffers"], system=UnitConverter.sys_pg):
            raise ValueError("effective_cache_size must not be below shared_buffers")
        if UnitConverter.size_from(
            config_res["min_wal_size"], system=UnitConverter.sys_pg
        ) > UnitConverter.size_from(config_res["max_wal_size"], system=UnitConverter.sys_pg):
            raise ValueError("min_wal_size must not exceed max_wal_size")
        if int(config_res["max_pred_locks_per_transaction"]) > int(
            config_res["max_locks_per_transaction"]
        ):
            raise ValueError(
                "max_pred_locks_per_transaction must not exceed max_locks_per_transaction"
            )
        for predicate_lock_setting in (
            "max_pred_locks_per_page",
            "max_pred_locks_per_relation",
        ):
            if int(config_res.get(predicate_lock_setting, 0)) > int(
                config_res["max_pred_locks_per_transaction"]
            ):
                raise ValueError(
                    f"{predicate_lock_setting} must not exceed max_pred_locks_per_transaction"
                )
        if int(config_res.get("autovacuum_worker_slots", 0)) and int(
            config_res["autovacuum_max_workers"]
        ) > int(config_res["autovacuum_worker_slots"]):
            raise ValueError("autovacuum_max_workers exceeds autovacuum_worker_slots")
        if int(config_res["default_statistics_target"]) not in statistics_tiers:
            raise ValueError("default_statistics_target must use a bounded statistics tier")
        if "jit" in config_res:
            if float(config_res["jit_inline_above_cost"]) < float(config_res["jit_above_cost"]):
                raise ValueError("jit_inline_above_cost must not be below jit_above_cost")
            if float(config_res["jit_optimize_above_cost"]) < float(config_res["jit_above_cost"]):
                raise ValueError("jit_optimize_above_cost must not be below jit_above_cost")
        if "io_max_combine_limit" in config_res and UnitConverter.size_from(
            config_res["io_combine_limit"], system=UnitConverter.sys_pg
        ) > UnitConverter.size_from(
            config_res["io_max_combine_limit"], system=UnitConverter.sys_pg
        ):
            raise ValueError("io_combine_limit must not exceed io_max_combine_limit")
        if (
            "vacuum_buffer_usage_limit" in config_res
            and UnitConverter.size_from(
                config_res["vacuum_buffer_usage_limit"], system=UnitConverter.sys_pg
            )
            > UnitConverter.size_from(config_res["shared_buffers"], system=UnitConverter.sys_pg) / 8
        ):
            raise ValueError("vacuum_buffer_usage_limit must not exceed shared_buffers / 8")
        if "profile_1c" in selected_profiles:
            if int(config_res["max_locks_per_transaction"]) < 512:
                raise ValueError("profile_1c requires at least 512 locks per transaction")
            if int(config_res["max_parallel_workers_per_gather"]) != 0:
                raise ValueError("profile_1c requires parallel query execution to be disabled")
            if config_res.get("enable_mergejoin") != "off":
                raise ValueError("profile_1c requires enable_mergejoin=off")
            if "jit" in config_res and config_res["jit"] != "off":
                raise ValueError("profile_1c requires jit=off")

        timeout_seconds = {
            "5s": 5,
            "10s": 10,
            "15s": 15,
            "1min": 60,
            "5min": 300,
            "10min": 600,
            "15min": 900,
            "30min": 1800,
            "1h": 3600,
            "2h": 7200,
            "4h": 14400,
            "8h": 28800,
        }
        if (
            timeout_seconds[config_res["lock_timeout"]]
            >= timeout_seconds[config_res["statement_timeout"]]
        ):
            raise ValueError("lock_timeout must be shorter than statement_timeout")
        if "transaction_timeout" in config_res and timeout_seconds[
            config_res["transaction_timeout"]
        ] <= max(
            timeout_seconds[config_res["statement_timeout"]],
            timeout_seconds[config_res["idle_in_transaction_session_timeout"]],
        ):
            raise ValueError(
                "transaction_timeout must exceed statement_timeout and "
                "idle_in_transaction_session_timeout"
            )

        actual_max_connections = int(config_res["max_connections"])
        actual_active_sessions = min(actual_max_connections, max(4, cpu_threads * 2))
        work_mem_bytes = UnitConverter.size_from(
            config_res["work_mem"], system=UnitConverter.sys_pg
        )
        temp_buffers_bytes = UnitConverter.size_from(
            config_res["temp_buffers"], system=UnitConverter.sys_pg
        )
        maintenance_work_mem_actual = UnitConverter.size_from(
            config_res["maintenance_work_mem"], system=UnitConverter.sys_pg
        )
        autovacuum_work_mem_actual = UnitConverter.size_from(
            config_res["autovacuum_work_mem"], system=UnitConverter.sys_pg
        )
        hash_multiplier_actual = float(config_res.get("hash_mem_multiplier", 1.0))
        client_memory_envelope = actual_active_sessions * (
            temp_buffers_bytes
            + work_mem_bytes * work_mem_concurrency_factor * hash_multiplier_actual
        )
        maintenance_memory_envelope = (
            maintenance_work_mem_actual * maint_max_conns
            + autovacuum_work_mem_actual * int(config_res["autovacuum_max_workers"])
        )
        logical_decoding_memory_envelope = 0
        if (
            replication_mode == ReplicationMode.LOGICAL
            and "logical_decoding_work_mem" in config_res
        ):
            logical_decoding_memory_envelope = UnitConverter.size_from(
                config_res["logical_decoding_work_mem"], system=UnitConverter.sys_pg
            ) * max(1, effective_logical_subscriptions)
        lock_process_count = (
            actual_max_connections
            + max_worker_processes
            + int(config_res["autovacuum_max_workers"])
            + int(config_res["max_wal_senders"])
        )
        lock_memory_envelope = (
            int(config_res["max_locks_per_transaction"]) * lock_process_count * 270
            + int(config_res["max_pred_locks_per_transaction"]) * actual_max_connections * 64
        )
        memory_envelope_bytes = (
            UnitConverter.size_from(config_res["shared_buffers"], system=UnitConverter.sys_pg)
            + client_memory_envelope
            + maintenance_memory_envelope
            + logical_decoding_memory_envelope
            + lock_memory_envelope
            + actual_max_connections * backend_memory_reserve_in_bytes
        )
        if memory_envelope_bytes > total_ram_in_bytes * 0.9:
            raise ValueError("Calculated concurrent memory envelope exceeds 90% of available RAM")

        # --- advisories ------------------------------------------------------
        # Every line below reads config_res, the file this run will actually
        # emit. Built any earlier they described a draft: profiles had not been
        # applied yet, so the text could promise synchronous_commit=on for a
        # cluster whose file says remote_apply, or call a statistics target high
        # after a profile had already lowered it.
        def size_text(value):
            return UnitConverter.size_to(int(value), system=UnitConverter.sys_pg)

        def settings_text(names):
            return ", ".join(f"{name}={config_res[name]}" for name in names)

        advisories = []

        if shared_buffers_part >= 0.35:
            advisories.append(
                advisory(
                    "shared_buffers_crowds_os_cache",
                    "warning",
                    f"shared_buffers_part={shared_buffers_part} gives "
                    f"shared_buffers={config_res['shared_buffers']}. Past roughly a third of "
                    "available RAM the same pages tend to be held twice, once here and once in "
                    "the kernel cache, and the second copy is the one that stops helping. The "
                    "0.35 threshold is this tool's, not a PostgreSQL limit.",
                    setting="shared_buffers",
                    actual=config_res["shared_buffers"],
                )
            )

        if disk_score is None:
            advisories.append(
                advisory(
                    "disk_score_inferred",
                    "assumption",
                    f"Storage score {disk_scores} was inferred from disk_type="
                    f"{disk_type.value}; nothing was measured. It sets random_page_cost, "
                    "effective_io_concurrency, the parallel-scan thresholds and the autovacuum "
                    "cost limits, so a disk_type that describes the hardware badly moves all of "
                    "them. Supply disk_score from measured IOPS and latency to replace the "
                    "guess.",
                    actual=disk_scores,
                )
            )

        # The multiplier the sizing divided by is not always a GUC in the file:
        # before PostgreSQL 13 there is no hash_mem_multiplier, and hash
        # aggregation had no spill to disk either, so the same reserve is kept
        # for a reason the reader cannot look up in the generated conf.
        hash_budget_text = (
            f"hash_mem_multiplier={config_res['hash_mem_multiplier']}"
            if "hash_mem_multiplier" in config_res
            else f"a factor of {hash_mem_multiplier} for hash operations, which on PostgreSQL "
            f"{pg_version} have neither a hash_mem_multiplier to declare nor, in hash "
            "aggregation, a spill to disk when the estimate is wrong"
        )
        advisories.append(
            advisory(
                "work_mem_budget_assumption",
                "assumption",
                f"work_mem={config_res['work_mem']} is the client memory budget divided by "
                f"{actual_active_sessions} concurrent sessions, "
                f"work_mem_concurrency_factor={work_mem_concurrency_factor} allocations each, "
                f"and {hash_budget_text}. All three are assumptions about the workload rather "
                "than measurements; pg_diag reports what the real figures are.",
                setting="work_mem",
                actual=config_res["work_mem"],
            )
        )

        if replication_enabled and desired_wal_keep_bytes > wal_keep_bytes:
            retention_setting = (
                "wal_keep_size" if "wal_keep_size" in config_res else "wal_keep_segments"
            )
            advisories.append(
                advisory(
                    "wal_retention_capped",
                    "warning",
                    f"Keeping {size_text(desired_wal_keep_bytes)} of WAL for a "
                    f"{replica_outage_tolerance}s outage would need more than the 40% of "
                    "wal_disk_budget this tool spends on retention, so "
                    f"{retention_setting}={config_res[retention_setting]} is what is kept — "
                    f"{wal_keep_segments} segments, "
                    f"{size_text(wal_keep_bytes + wal_segment_size_in_bytes)} on disk once the "
                    "segment being written is counted. A replica absent for longer needs a "
                    "fresh base backup: raise wal_disk_budget or lower "
                    "replica_outage_tolerance.",
                    setting=retention_setting,
                    actual=config_res[retention_setting],
                )
            )

        if duty_db == DutyDB.FINANCIAL and not synchronous_standby_names.strip():
            advisories.append(
                advisory(
                    "financial_duty_without_synchronous_standby",
                    "info",
                    "Financial duty asks for the strongest durability on offer, but no "
                    "synchronous_standby_names were supplied, so "
                    f"synchronous_commit={config_res['synchronous_commit']} is as far as it "
                    "goes: a commit is flushed to local disk and nothing remote is promised. "
                    "Name a standby to get remote_apply.",
                    setting="synchronous_commit",
                    actual=config_res["synchronous_commit"],
                )
            )

        if config_res["wal_level"] == "minimal":
            advisories.append(
                advisory(
                    "wal_level_minimal",
                    "info",
                    "wal_level=minimal follows from asking for neither PITR nor replication. "
                    "Crash recovery still works and committed transactions still survive a "
                    "restart; what becomes impossible is point-in-time recovery, streaming a "
                    "replica, and taking an online base backup. Losing the data directory then "
                    "means restoring a cold copy.",
                    setting="wal_level",
                    actual=config_res["wal_level"],
                )
            )

        end_of_life_date = self.postgresql_eol_dates.get(pg_version)
        horizon_year, horizon_remainder = self.support_horizon.split("-", 1)
        horizon_next_year = f"{int(horizon_year) + 1}-{horizon_remainder}"
        if end_of_life_date is not None and end_of_life_date <= self.support_horizon:
            advisories.append(
                advisory(
                    "postgresql_end_of_life",
                    "warning",
                    f"PostgreSQL {pg_version} reached end of life on {end_of_life_date}: no "
                    "further fixes are published for it, security ones included. This output "
                    "is for legacy and test use.",
                    actual=end_of_life_date,
                )
            )
        elif end_of_life_date is not None and end_of_life_date <= horizon_next_year:
            advisories.append(
                advisory(
                    "postgresql_end_of_life_approaching",
                    "info",
                    f"PostgreSQL {pg_version} reaches end of life on {end_of_life_date}, "
                    f"within a year of this tool's support horizon of {self.support_horizon}. "
                    "The major upgrade wants planning before then, not after.",
                    actual=end_of_life_date,
                )
            )

        if pitr_enabled:
            advisories.append(
                advisory(
                    "pitr_transport_not_configured",
                    "info",
                    f"pitr_enabled holds wal_level={config_res['wal_level']}, which is the "
                    "part of point-in-time recovery a configuration file can supply. Base "
                    "backups, WAL archiving and the restore path are deployment work this tool "
                    "does not do: pg_stand is one way to arrange it, pgBackRest and barman are "
                    "others.",
                    setting="wal_level",
                    actual=config_res["wal_level"],
                )
            )

        network_settings = [
            name
            for name in (
                "tcp_keepalives_idle",
                "tcp_keepalives_interval",
                "tcp_keepalives_count",
                "tcp_user_timeout",
                "client_connection_check_interval",
            )
            if name in config_res
        ]
        advisories.append(
            advisory(
                "network_timeouts_are_baselines",
                "info",
                f"PostgreSQL {pg_version} on {platform.value.lower()} takes "
                f"{settings_text(network_settings)} from this file. They are baselines: a dead "
                "connection is noticed only as fast as the slowest layer in front of it allows, "
                "so align them with the load balancer, firewall, proxy and client-driver "
                "timeouts before applying.",
            )
        )

        workload_timeouts = [
            name
            for name in (
                "statement_timeout",
                "lock_timeout",
                "idle_in_transaction_session_timeout",
                "idle_session_timeout",
                "transaction_timeout",
            )
            if name in config_res
        ]
        advisories.append(
            advisory(
                "workload_timeouts_are_instance_wide",
                "info",
                f"This file sets {settings_text(workload_timeouts)} for the whole instance, "
                "maintenance and DBA sessions included, which suits a reproducible stand. In "
                "production the documented practice is ALTER ROLE and ALTER DATABASE, keeping "
                "one role less restricted so that a long recovery task can still run.",
            )
        )

        if config_res.get("password_encryption") == "scram-sha-256":
            advisories.append(
                advisory(
                    "scram_password_encryption",
                    "info",
                    "password_encryption=scram-sha-256 applies to passwords stored from now on; "
                    "existing md5 passwords keep working until they are set again. Check that "
                    "every client driver in use speaks SCRAM before rotating them.",
                    setting="password_encryption",
                    actual=config_res["password_encryption"],
                )
            )

        if "idle_session_timeout" in config_res:
            advisories.append(
                advisory(
                    "idle_session_timeout_and_poolers",
                    "info",
                    f"idle_session_timeout={config_res['idle_session_timeout']} closes idle "
                    "sessions, including the ones a connection pooler is holding open on "
                    "purpose. Verify that the pooler reconnects cleanly, or scope this timeout "
                    "to interactive roles.",
                    setting="idle_session_timeout",
                    actual=config_res["idle_session_timeout"],
                )
            )

        if platform == Platform.WINDOWS:
            # Only settings this version actually emits may be described. Saying
            # a parameter "remains 0" on a release that has never had it is how
            # the previous text managed to be wrong on 9.6 through 13.
            system_default_settings = [
                name for name in ("tcp_keepalives_count", "tcp_user_timeout") if name in config_res
            ]
            windows_text = (
                "Windows exposes neither TCP_KEEPCNT nor TCP_USER_TIMEOUT, so "
                f"{settings_text(system_default_settings)} leaves detection of a dead peer to "
                "the operating-system defaults."
            )
            if "client_connection_check_interval" in config_res:
                windows_text += (
                    " client_connection_check_interval=0 is not a system default but the check "
                    "switched off: PostgreSQL implements it only where poll() offers POLLRDHUP, "
                    "which Windows does not, so a backend keeps running a query for a client "
                    "that has already gone."
                )
            advisories.append(advisory("windows_network_options_unavailable", "info", windows_text))

        if database_size_in_bytes is None:
            advisories.append(
                advisory(
                    "db_size_not_supplied",
                    "assumption",
                    "db_size was not supplied, so default_statistics_target="
                    f"{config_res['default_statistics_target']} comes from duty and hardware "
                    "alone, with no database-size tier applied.",
                    setting="default_statistics_target",
                    actual=config_res["default_statistics_target"],
                )
            )

        if int(config_res["default_statistics_target"]) >= 2500:
            advisories.append(
                advisory(
                    "high_statistics_target",
                    "info",
                    f"default_statistics_target={config_res['default_statistics_target']} makes "
                    "ANALYZE read more rows and the planner carry longer histograms, which costs "
                    "planning time on every query rather than only on the skewed ones. Where the "
                    "skew is in a few columns, ALTER TABLE ... ALTER COLUMN ... SET STATISTICS "
                    "is the cheaper instrument.",
                    setting="default_statistics_target",
                    actual=config_res["default_statistics_target"],
                )
            )

        if "profile_1c" in selected_profiles:
            advisories.append(
                advisory(
                    "profile_1c_ssl_disabled",
                    "warning",
                    f"profile_1c sets ssl={config_res['ssl']}. Traffic between the 1C server "
                    "and PostgreSQL is unencrypted, so the link between them has to be one you "
                    "already trust.",
                    setting="ssl",
                    actual=config_res["ssl"],
                )
            )
            advisories.append(
                advisory(
                    "profile_1c_row_security_disabled",
                    "warning",
                    f"profile_1c sets row_security={config_res['row_security']}. This does not "
                    "read past row-level security: a query that would have a policy applied "
                    "fails with an error instead, unless the role owns the table or holds "
                    "BYPASSRLS. Any RLS in the database turns into an outage here, not a "
                    "bypass.",
                    setting="row_security",
                    actual=config_res["row_security"],
                )
            )
            advisories.append(
                advisory(
                    "profile_1c_standard_conforming_strings_disabled",
                    "warning",
                    "profile_1c sets standard_conforming_strings="
                    f"{config_res['standard_conforming_strings']}. Backslashes inside ordinary "
                    "string literals become escape characters again, so existing SQL can parse "
                    "into something else and an escaping mistake becomes exploitable. It is "
                    "here because 1C emits literals written for that dialect.",
                    setting="standard_conforming_strings",
                    actual=config_res["standard_conforming_strings"],
                )
            )
            requested_1c_connections = 1000
            binding_limits = []
            if actual_max_connections == requested_1c_connections:
                binding_limits.append(f"its own request of {requested_1c_connections}")
            if actual_max_connections == connection_capacity:
                binding_limits.append(f"the memory budget, which holds {connection_capacity}")
            if actual_max_connections == max_conns:
                binding_limits.append(f"max_conns={max_conns}")
            advisories.append(
                advisory(
                    "profile_1c_connection_target",
                    "info",
                    f"profile_1c asks for {requested_1c_connections} connections and "
                    f"max_connections={actual_max_connections} is what came out, bound by "
                    + (" and ".join(binding_limits) or "a rule from a later profile")
                    + ". A connection costs memory whether or not it is running a query, so a "
                    "pooler in front of the database is what keeps this number affordable.",
                    setting="max_connections",
                    actual=actual_max_connections,
                )
            )
            advisories.append(
                advisory(
                    "profile_1c_file_limit_raised",
                    "info",
                    "profile_1c sets max_files_per_process="
                    f"{config_res['max_files_per_process']}, well above the default. The "
                    "operating-system limit on open files has to be raised to match before this "
                    "is applied, or backends start failing to open relations under load.",
                    setting="max_files_per_process",
                    actual=config_res["max_files_per_process"],
                )
            )
            if config_res["synchronous_commit"] == "remote_apply":
                synchronous_commit_text = (
                    "synchronous_commit=remote_apply: financial duty with a named standby "
                    "outranks the 1C performance guidance, so a commit waits for the standby to "
                    "apply it, not merely to receive it. That is the slowest and safest of the "
                    "settings on offer."
                )
            else:
                synchronous_commit_text = (
                    f"synchronous_commit={config_res['synchronous_commit']}: every commit is "
                    "flushed to local disk before it is acknowledged. 1C performance guidance "
                    "permits turning this off and losing recent transactions on a crash; this "
                    "tool does not."
                )
            advisories.append(
                advisory(
                    "profile_1c_synchronous_commit",
                    "info",
                    f"profile_1c leaves {synchronous_commit_text}",
                    setting="synchronous_commit",
                    actual=config_res["synchronous_commit"],
                )
            )
            advisories.append(
                advisory(
                    "profile_1c_patched_gucs_omitted",
                    "info",
                    "profile_1c emits nothing that only a patched PostgreSQL understands, such "
                    "as enable_temp_memory_catalog. A build that has those settings will not "
                    "receive them from here without an explicit target-distribution contract.",
                )
            )

        if required_extensions:
            if normalized_available_extensions is None:
                advisories.append(
                    advisory(
                        "preload_modules_not_declared",
                        "assumption",
                        "Nothing was declared about what the target has installed, so "
                        f"shared_preload_libraries={config_res['shared_preload_libraries']} is a "
                        "requirement this run could not check. These are libraries loaded at "
                        "startup rather than CREATE EXTENSION objects — auto_explain has no "
                        "SQL-level extension at all — and a missing one keeps the server from "
                        "starting. Pass --available-extensions to assert an inventory.",
                        setting="shared_preload_libraries",
                        actual=config_res["shared_preload_libraries"],
                    )
                )
            else:
                advisories.append(
                    advisory(
                        "extension_inventory_not_verified",
                        "assumption",
                        "The inventory is what the caller declared, not what the target "
                        "answered: shared_preload_libraries="
                        f"{config_res['shared_preload_libraries']} was accepted on that word "
                        "alone. Packaging, preloadability and the GUCs those modules add still "
                        "have to be checked against the real server before this file is "
                        "applied.",
                        setting="shared_preload_libraries",
                        actual=config_res["shared_preload_libraries"],
                    )
                )

        advisories.append(
            advisory(
                "csv_log_retention_is_external",
                "info",
                f"log_destination={config_res['log_destination']} with rotation by size and by "
                "time. Rotation renames files; it never deletes them. A total disk limit for "
                "the log directory has to come from outside PostgreSQL.",
                setting="log_destination",
                actual=config_res["log_destination"],
            )
        )

        advisories = sort_advisories(advisories)

        self.last_inputs = {
            "available_extensions": (
                sorted(normalized_available_extensions)
                if normalized_available_extensions is not None
                else None
            ),
            "common_conf": common_conf,
            "cpu_cores": total_cpu_cores,
            "disk_score": disk_scores,
            "disk_score_source": "explicit" if disk_score is not None else "disk_type",
            "disk_type": disk_type.value,
            "db_size_bytes": (
                int(database_size_in_bytes) if database_size_in_bytes is not None else None
            ),
            "duty_db": duty_db.value,
            "logical_subscription_count": effective_logical_subscriptions,
            "peak_wal_rate_bytes_per_second": peak_wal_rate_in_bytes,
            "pitr_enabled": pitr_enabled,
            "pg_version": pg_version,
            "profiles": selected_profiles,
            "ram_bytes": ram_in_bytes,
            "replica_count": effective_replica_count,
            "replica_outage_tolerance_seconds": replica_outage_tolerance,
            "replication_enabled": replication_enabled,
            "replication_mode": replication_mode.value,
            "reserved_ram_percent": reserved_ram_percent,
            "reserved_system_ram_bytes": reserved_system_ram_in_bytes,
            "synchronous_standby_names": synchronous_standby_names,
            "wal_disk_budget_bytes": wal_disk_budget_in_bytes,
            "wal_segment_size_bytes": wal_segment_size_in_bytes,
            "work_mem_concurrency_factor": work_mem_concurrency_factor,
        }
        self.last_extensions = [
            {
                "availability": (
                    "declared_available"
                    if normalized_available_extensions is not None
                    and extension in normalized_available_extensions
                    else "unverified"
                ),
                "availability_source": (
                    "caller_inventory" if normalized_available_extensions is not None else None
                ),
                "name": extension,
                "provider": EXTENSION_SPECS[extension]["provider"],
                "settings_validation": EXTENSION_SPECS[extension]["settings_validation"],
                "supported_versions": list(EXTENSION_SPECS[extension]["supported_versions"]),
            }
            for extension in EXTENSION_PRELOAD_ORDER
            if extension in required_extensions
        ]
        self.last_calculation = {
            "active_query_sessions": actual_active_sessions,
            "available_ram_bytes": int(total_ram_in_bytes),
            "autovacuum_worker_budget": autovacuum_workers,
            "autovacuum_naptime": autovacuum_naptime,
            "autovacuum_analyze_scale_factor": autovacuum_analyze_scale_factor,
            "autovacuum_vacuum_scale_factor": autovacuum_vacuum_scale_factor,
            "checkpoint_timeout_seconds": checkpoint_timeout_seconds,
            "client_memory_envelope_bytes": int(client_memory_envelope),
            "connection_capacity": connection_capacity,
            "connections_per_cpu": connections_per_cpu,
            "cpu_score": round(cpu_scores, 4),
            "disk_score": round(disk_scores, 4),
            "effective_io_concurrency": effective_io_concurrency,
            "effective_cache_size_bytes": int(effective_cache_size_bytes),
            "default_statistics_target": default_statistics_target,
            "statistics_resource_cap": statistics_resource_cap,
            "statistics_size_multiplier": statistics_size_multiplier,
            "profile_1c_statistics_target": profile_1c_statistics_target,
            "profile_backend_statistics_target": profile_backend_statistics_target,
            "parallel_setup_cost": parallel_setup_cost,
            "parallel_tuple_cost": parallel_tuple_cost,
            "min_parallel_table_scan_size": min_parallel_table_scan_size,
            "min_parallel_index_scan_size": min_parallel_index_scan_size,
            "io_combine_limit_bytes": int(io_combine_limit_bytes),
            "io_max_combine_limit_bytes": int(io_max_combine_limit_bytes),
            "vacuum_buffer_usage_limit_bytes": int(vacuum_buffer_usage_limit_bytes),
            "logical_worker_budget": logical_worker_budget,
            "logical_decoding_memory_envelope_bytes": int(logical_decoding_memory_envelope),
            "lock_memory_envelope_bytes": int(lock_memory_envelope),
            "maintenance_memory_envelope_bytes": int(maintenance_memory_envelope),
            "memory_envelope_bytes": int(memory_envelope_bytes),
            "parallel_worker_budget": parallel_worker_budget,
            "ram_score": round(ram_scores, 4),
            "network_failure_detection_seconds": network_failure_detection_seconds,
            "replication_network_timeout_seconds": int(
                replication_network_timeout.removesuffix("s")
            ),
            "replication_slot_budget": replication_slot_budget,
            "wal_keep_bytes": int(wal_keep_bytes),
            "wal_sender_budget": wal_sender_budget,
            "worker_process_budget": worker_process_budget,
            "work_mem_cap_bytes": duty_work_mem_cap_bytes,
        }
        self.last_parameter_details = dict(sorted(parameter_details.items()))
        self.last_overrides = overrides
        self.last_advisories = advisories
        return config_res

    def settings_history(self, list_versions) -> PGConfiguratorResult:
        if len(list_versions) != 2:
            raise ValueError("settings_history requires exactly two PostgreSQL versions")
        unknown_versions = [
            version for version in list_versions if version not in self.known_versions
        ]
        if unknown_versions:
            raise ValueError("Unknown PostgreSQL version: {}".format(", ".join(unknown_versions)))

        res = PGConfiguratorResult()
        configs = []
        for v in sorted([ver for ver in list_versions], key=lambda x: get_major_version(x)):
            with open(
                os.path.join(self.current_dir, "pg_settings_history", self.known_versions[v]),
                encoding="utf-8",
            ) as f:
                reader = csv.reader(f)
                next(reader, None)  # skip header
                conf = {}
                for row in reader:
                    conf[row[0]] = {"value": row[1], "boot_val": row[3], "unit": row[4]}
                configs.append(conf)

        res.result_code = ResultCode.DONE
        res.result_data = {
            "Deprecated parameters": {
                v: configs[0][v] for v in [k for k, _ in configs[0].items() if k not in configs[1]]
            },
            "New parameters": {
                v: configs[1][v] for v in [k for k, _ in configs[1].items() if k not in configs[0]]
            },
            "Changed boot_val": {
                k: {"old": configs[0][k]["boot_val"], "new": v["boot_val"]}
                for k, v in configs[1].items()
                if k in configs[0] and v["boot_val"] != configs[0][k]["boot_val"]
            },
            "Changed unit": {
                k: {"old": configs[0][k]["unit"], "new": v["unit"]}
                for k, v in configs[1].items()
                if k in configs[0] and v["unit"] != configs[0][k]["unit"]
            },
        }
        res.artifact = {
            "schema_version": "pg_configurator/settings-history-v1",
            "generator": {"name": "pg-configurator", "version": __version__},
            "versions": list_versions,
            "differences": res.result_data,
        }
        return res

    def specific_setting_history(self, setting_name) -> PGConfiguratorResult:
        if not setting_name or not isinstance(setting_name, str):
            raise ValueError("setting_name must be a non-empty string")

        res = PGConfiguratorResult()
        configs = []
        for v in sorted([ver for ver in self.known_versions], key=lambda x: get_major_version(x)):
            with open(
                os.path.join(self.current_dir, "pg_settings_history", self.known_versions[v]),
                encoding="utf-8",
            ) as f:
                reader = csv.reader(f)
                next(reader, None)  # skip header
                conf = {}
                for row in reader:
                    if row[0] == setting_name:
                        conf[v] = {
                            "setting": row[0],
                            "value": row[1],
                            "boot_val": row[3],
                            "unit": row[4],
                        }
                        continue
                if len(conf) == 0:
                    conf[v] = {"setting": "not exists", "value": "", "boot_val": "", "unit": ""}
                configs.append(conf)

        res.result_code = ResultCode.DONE
        res.result_data = configs
        res.artifact = {
            "schema_version": "pg_configurator/setting-history-v1",
            "generator": {"name": "pg-configurator", "version": __version__},
            "setting": setting_name,
            "history": configs,
        }
        return res

    def build_artifact(self, config):
        artifact = {
            "schema_version": "pg_configurator/v2",
            "kind": "PostgreSQLConfiguration",
            "generator": {
                "name": "pg-configurator",
                "version": __version__,
            },
            "generated_at": datetime.datetime.now(datetime.timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "inputs": self.last_inputs,
            "extensions": self.last_extensions,
            "calculation": self.last_calculation,
            "parameters": self.last_parameter_details,
            "overrides": self.last_overrides,
            "advisories": self.last_advisories,
            "postgresql_conf": config,
        }
        artifact["artifact_hash"] = artifact_hash(artifact)
        self.last_artifact = artifact
        return artifact

    @staticmethod
    def get_arg_parser():
        parser = argparse.ArgumentParser()
        mca = get_default_args(PGConfigurator.make_conf)

        parser.add_argument(
            "--version", help="Show the version number and exit", action="store_true", default=False
        )
        parser.add_argument(
            "--capabilities",
            "--component-capabilities",
            dest="capabilities",
            help=argparse.SUPPRESS,
            action="store_true",
            default=False,
        )
        parser.add_argument(
            "--machine",
            help=argparse.SUPPRESS,
            action="store_true",
            default=False,
        )
        parser.add_argument(
            "--request-id",
            help=argparse.SUPPRESS,
            default=None,
        )
        parser.add_argument(
            "--input-json",
            help=argparse.SUPPRESS,
            default=None,
        )
        parser.add_argument(
            "--validate-input",
            help=argparse.SUPPRESS,
            action="store_true",
            default=False,
        )
        parser.add_argument(
            "--debug",
            help="Enable debug mode, (default: %(default)s)",
            action="store_true",
            default=False,
        )
        parser.add_argument(
            "--output-format",
            help="Specify output format, (default: %(default)s)",
            type=OutputFormat,
            choices=list(OutputFormat),
            default=OutputFormat.CONF.value,
        )
        parser.add_argument(
            "--out",
            "--output-file-name",
            dest="output_file_name",
            help="Save to an exact output file (--output-file-name is a compatibility alias)",
            type=str,
            default="",
        )
        parser.add_argument(
            "--db-cpu",
            help="Available CPU cores; decimal cores and millicores such as 500m are accepted",
            type=str,
            default=psutil.cpu_count(),
        )
        parser.add_argument(
            "--db-ram",
            help="Physical RAM with an IEC suffix such as Mi, Gi, or Ti",
            type=str,
            default=UnitConverter.size_to(
                psutil.virtual_memory().total, system=UnitConverter.sys_iec
            ),
        )
        parser.add_argument(
            "--db-size",
            help=(
                "Optional logical database size with an IEC suffix; selects a bounded "
                "default_statistics_target tier"
            ),
            type=str,
            default=mca["db_size"],
        )
        parser.add_argument(
            "--db-disk-type",
            help="Disks type, (default: %(default)s)",
            type=DiskType,
            choices=list(DiskType),
            default=mca["disk_type"].value,
        )
        parser.add_argument(
            "--disk-score",
            help="Measured composite storage score from 0 to 100",
            type=float,
            default=mca["disk_score"],
        )
        parser.add_argument(
            "--db-duty",
            help="Database duty, (default: %(default)s)",
            type=DutyDB,
            choices=list(DutyDB),
            default=mca["duty_db"].value,
        )
        parser.add_argument(
            "--replication-enabled",
            help=(
                "Compatibility switch: true maps to physical and false to none; when both "
                "replication options are explicit they must agree"
            ),
            type=parse_bool,
            default=mca["replication_enabled"],
        )
        parser.add_argument(
            "--replication-mode",
            help="Required replication capability: none, physical, or logical",
            type=ReplicationMode,
            choices=list(ReplicationMode),
            default=mca["replication_mode"],
        )
        parser.add_argument(
            "--pitr-enabled",
            help=(
                "Keep wal_level PITR-compatible; backup and archive transport remain a "
                "pg_stand responsibility, (default: %(default)s)"
            ),
            type=parse_bool,
            default=mca["pitr_enabled"],
        )
        parser.add_argument(
            "--synchronous-standby-names",
            help="Value for synchronous_standby_names; enables truthful remote_apply",
            type=str,
            default=mca["synchronous_standby_names"],
        )
        parser.add_argument(
            "--replica-count",
            help="Expected physical replicas, (default: %(default)s)",
            type=int,
            default=mca["replica_count"],
        )
        parser.add_argument(
            "--logical-subscription-count",
            help="Expected logical subscriptions on this node, (default: %(default)s)",
            type=int,
            default=mca["logical_subscription_count"],
        )
        parser.add_argument(
            "--pg-version",
            help="PostgreSQL version, (default: %(default)s)",
            type=str,
            choices=list(["9.6", "10", "11", "12", "13", "14", "15", "16", "17", "18"]),
            default=mca["pg_version"],
        )
        parser.add_argument(
            "--reserved-ram-percent",
            help="Reserved RAM memory part, (default: %(default)s)",
            type=float,
            default=mca["reserved_ram_percent"],
        )
        parser.add_argument(
            "--reserved-system-ram",
            help="Reserved system RAM memory, (default: %(default)s)",
            type=str,
            default=mca["reserved_system_ram"],
        )
        parser.add_argument(
            "--shared-buffers-part",
            help="Shared buffers part, (default: %(default)s)",
            type=float,
            default=mca["shared_buffers_part"],
        )
        parser.add_argument(
            "--client-mem-part",
            help="RAM fraction for concurrently active query memory, (default: %(default)s)",
            type=float,
            default=mca["client_mem_part"],
        )
        parser.add_argument(
            "--maintenance-mem-part",
            help="RAM fraction shared by maintenance and autovacuum, (default: %(default)s)",
            type=float,
            default=mca["maintenance_mem_part"],
        )
        parser.add_argument(
            "--autovacuum-workers-mem-part",
            help="Memory part of maintenance-mem, (default: %(default)s)",
            type=float,
            default=mca["autovacuum_workers_mem_part"],
        )
        parser.add_argument(
            "--maintenance-conns-mem-part",
            help="Memory part of maintenance-mem, (default: %(default)s)",
            type=float,
            default=mca["maintenance_conns_mem_part"],
        )
        parser.add_argument(
            "--work-mem-concurrency-factor",
            help=(
                "Combined work_mem amplification for concurrent operators, parallel workers, "
                "and hash operations, (default: %(default)s)"
            ),
            type=float,
            default=mca["work_mem_concurrency_factor"],
        )
        parser.add_argument(
            "--peak-wal-rate",
            help="Expected peak WAL generation per second, (default: %(default)s)",
            type=str,
            default=mca["peak_wal_rate"],
        )
        parser.add_argument(
            "--replica-outage-tolerance",
            help="WAL retention target in seconds, (default: %(default)s)",
            type=int,
            default=mca["replica_outage_tolerance"],
        )
        parser.add_argument(
            "--wal-disk-budget",
            help="Total disk budget for pg_wal and retained WAL, (default: %(default)s)",
            type=str,
            default=mca["wal_disk_budget"],
        )
        parser.add_argument(
            "--wal-segment-size",
            help="Actual cluster WAL segment size used for WAL sizing, (default: %(default)s)",
            type=str,
            default=mca["wal_segment_size"],
        )
        parser.add_argument(
            "--min-conns",
            help="Min client connection, (default: %(default)s)",
            type=int,
            default=mca["min_conns"],
        )
        parser.add_argument(
            "--max-conns",
            help="Max client connection, (default: %(default)s)",
            type=int,
            default=mca["max_conns"],
        )
        parser.add_argument(
            "--min-autovac-workers",
            help="Min autovacuum workers, (default: %(default)s)",
            type=int,
            default=mca["min_autovac_workers"],
        )
        parser.add_argument(
            "--max-autovac-workers",
            help="Max autovacuum workers, (default: %(default)s)",
            type=int,
            default=mca["max_autovac_workers"],
        )
        parser.add_argument(
            "--min-maint-conns",
            help="Min maintenance connections, (default: %(default)s)",
            type=int,
            default=mca["min_maint_conns"],
        )
        parser.add_argument(
            "--max-maint-conns",
            help="Max maintenance connections, (default: %(default)s)",
            type=int,
            default=mca["max_maint_conns"],
        )
        parser.add_argument(
            "--common-conf",
            help=(
                "Keep the mandatory version-aware logging, statistics, and observability "
                "configuration enabled; --no-common-conf is rejected"
            ),
            action=argparse.BooleanOptionalAction,
            default=mca["common_conf"],
        )
        parser.add_argument(
            "--platform",
            help="Target OS; controls availability of TCP timeout features, (default: %(default)s)",
            type=Platform,
            choices=list(Platform),
            default=mca["platform"].value,
        )
        parser.add_argument(
            "--settings-history",
            help=(
                "Show pg_settings differences between two versions, "
                "for example --settings-history=9.6,15"
            ),
            type=str,
            default="",
        )
        parser.add_argument(
            "--conf-profiles",
            help="Select one or more comma-separated configuration profiles",
            type=str,
            default="",
        )
        parser.add_argument(
            "--available-extensions",
            help=(
                "Caller-declared extension inventory for the selected target major; this "
                "command does not perform a live target preflight"
            ),
            type=str,
            default=None,
        )
        parser.add_argument(
            "--specific-setting-history",
            help=(
                "Show one setting across versions, for example "
                "--specific-setting-history=max_parallel_maintenance_workers"
            ),
            type=str,
            default="",
        )

        return parser


_ORCHESTRATION_ONLY_DESTINATIONS = {
    "capabilities",
    "debug",
    "input_json",
    "machine",
    "output_file_name",
    "request_id",
    "settings_history",
    "specific_setting_history",
    "validate_input",
    "version",
}


def _input_json_path(arguments: list[str]) -> str | None:
    for index, argument in enumerate(arguments):
        if argument.startswith("--input-json="):
            return argument.partition("=")[2]
        if argument == "--input-json":
            if index + 1 >= len(arguments):
                raise ValueError("--input-json requires a file path or - for stdin")
            return arguments[index + 1]
    return None


def _arguments_from_input_json(parser: argparse.ArgumentParser, arguments: list[str]) -> list[str]:
    input_path = _input_json_path(arguments)
    if input_path is None:
        return arguments
    if input_path == "-":
        document = json.load(sys.stdin)
    else:
        with open(input_path, encoding="utf-8") as input_file:
            document = json.load(input_file)
    if not isinstance(document, dict):
        raise ValueError("--input-json must contain a JSON object")
    if "inputs" in document:
        schema_version = document.get("schema_version")
        if schema_version != "pg_configurator/input-v1":
            raise ValueError(
                "JSON input with an inputs field requires schema_version=pg_configurator/input-v1"
            )
        document = document["inputs"]
        if not isinstance(document, dict):
            raise ValueError("input JSON field inputs must be an object")

    actions = {
        action.dest: action
        for action in parser._actions
        if action.option_strings and action.dest not in _ORCHESTRATION_ONLY_DESTINATIONS
    }
    normalized_document = {str(key).replace("-", "_"): value for key, value in document.items()}
    unknown = sorted(set(normalized_document).difference(actions))
    if unknown:
        raise ValueError("unknown input JSON field(s): " + ", ".join(unknown))

    generated: list[str] = []
    for destination in sorted(normalized_document):
        value = normalized_document[destination]
        if value is None:
            continue
        action = actions[destination]
        positive_option = next(
            (
                option
                for option in action.option_strings
                if option.startswith("--") and not option.startswith("--no-")
            ),
            action.option_strings[0],
        )
        if isinstance(action, argparse.BooleanOptionalAction):
            if not isinstance(value, bool):
                raise ValueError(f"input JSON field {destination} must be boolean")
            generated.append(positive_option if value else "--no-" + positive_option[2:])
        elif isinstance(value, bool):
            generated.extend([positive_option, "true" if value else "false"])
        elif isinstance(value, (dict, list)):
            raise ValueError(f"input JSON field {destination} must be a scalar value")
        else:
            generated.extend([positive_option, str(value)])
    # Generated arguments precede the original command line so explicitly
    # supplied CLI options retain their familiar last-value-wins behavior.
    return [*generated, *arguments]


def run_pgc(external_args=None, ext_params=None) -> PGConfiguratorResult:
    parser = PGConfigurator.get_arg_parser()
    if external_args is None:
        args = parser.parse_args(_arguments_from_input_json(parser, sys.argv[1:]))
    elif isinstance(external_args, (list, tuple)):
        args = parser.parse_args(_arguments_from_input_json(parser, list(external_args)))
    else:
        args = external_args

    pgc = PGConfigurator(args, ext_params)
    if args.debug:
        print(
            "{} pg-configurator started".format(datetime.datetime.now().isoformat(" ")),
            file=sys.stderr,
        )

    if args.capabilities:
        payload = capabilities()
        if args.machine:
            payload = envelope(
                "capabilities",
                "succeeded",
                request_id=args.request_id,
                result=payload,
            )
        _write_output(json.dumps(payload, indent=2, sort_keys=True) + "\n", args.output_file_name)
        return PGConfiguratorResult(
            result_code=ResultCode.DONE,
            result_data=payload,
            artifact=payload,
        )

    if args.version:
        result = PGConfiguratorResult(
            result_code=ResultCode.DONE,
            result_data={"version": __version__},
        )
        _write_output(f"pg-configurator {__version__}\n", args.output_file_name)
        return result

    if args.settings_history:
        result = pgc.settings_history(
            [version.strip() for version in args.settings_history.split(",")]
        )
        _write_output(
            json.dumps(result.artifact, indent=2, sort_keys=True) + "\n",
            args.output_file_name,
        )
        return result

    if args.specific_setting_history:
        result = pgc.specific_setting_history(args.specific_setting_history)
        _write_output(
            json.dumps(result.artifact, indent=2, sort_keys=True) + "\n",
            args.output_file_name,
        )
        return result

    conf = pgc.make_conf(
        args.db_cpu,
        args.db_ram,
        disk_type=args.db_disk_type,
        duty_db=args.db_duty,
        replication_enabled=args.replication_enabled,
        replication_mode=args.replication_mode,
        pitr_enabled=args.pitr_enabled,
        synchronous_standby_names=args.synchronous_standby_names,
        replica_count=args.replica_count,
        logical_subscription_count=args.logical_subscription_count,
        pg_version=args.pg_version,
        reserved_ram_percent=args.reserved_ram_percent,
        reserved_system_ram=args.reserved_system_ram,
        shared_buffers_part=args.shared_buffers_part,
        client_mem_part=args.client_mem_part,
        maintenance_mem_part=args.maintenance_mem_part,
        autovacuum_workers_mem_part=args.autovacuum_workers_mem_part,
        maintenance_conns_mem_part=args.maintenance_conns_mem_part,
        min_conns=args.min_conns,
        max_conns=args.max_conns,
        min_autovac_workers=args.min_autovac_workers,
        max_autovac_workers=args.max_autovac_workers,
        min_maint_conns=args.min_maint_conns,
        max_maint_conns=args.max_maint_conns,
        platform=args.platform,
        common_conf=args.common_conf,
        conf_profiles=args.conf_profiles,
        disk_score=args.disk_score,
        work_mem_concurrency_factor=args.work_mem_concurrency_factor,
        peak_wal_rate=args.peak_wal_rate,
        replica_outage_tolerance=args.replica_outage_tolerance,
        wal_disk_budget=args.wal_disk_budget,
        wal_segment_size=args.wal_segment_size,
        available_extensions=args.available_extensions,
        db_size=args.db_size,
    )
    if args.debug:
        print(
            "# normalized_inputs = " + json.dumps(pgc.last_inputs, sort_keys=True),
            file=sys.stderr,
        )
    artifact = pgc.build_artifact(conf)
    if args.validate_input:
        validation = {
            "valid": True,
            "normalized_inputs": pgc.last_inputs,
            "advisories": list(pgc.last_advisories),
            "candidate_hash": artifact["artifact_hash"],
        }
        payload = (
            envelope(
                "validate-input",
                "succeeded",
                request_id=args.request_id,
                result=validation,
                advisories=list(pgc.last_advisories),
            )
            if args.machine
            else validation
        )
        _write_output(json.dumps(payload, indent=2, sort_keys=True) + "\n", args.output_file_name)
        return PGConfiguratorResult(
            result_code=ResultCode.DONE,
            result_data=validation,
            artifact=artifact,
            advisories=list(pgc.last_advisories),
        )
    output_format = (
        args.output_format
        if isinstance(args.output_format, OutputFormat)
        else OutputFormat(args.output_format)
    )

    if args.machine:
        machine_result = {
            "output_format": output_format.value,
            "artifact": artifact,
        }
        if output_format == OutputFormat.PATRONI_JSON:
            machine_result["document"] = _patroni_document(conf)
        payload = envelope(
            "generate",
            "succeeded",
            request_id=args.request_id,
            result=machine_result,
            artifacts=[
                {
                    "kind": "PostgreSQLConfiguration",
                    "schema_version": artifact["schema_version"],
                    "hash": artifact["artifact_hash"],
                    "path": (
                        os.path.abspath(args.output_file_name) if args.output_file_name else None
                    ),
                }
            ],
            advisories=list(pgc.last_advisories),
        )
        output = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    elif output_format == OutputFormat.CONF:
        output = _render_postgresql_conf(conf, artifact)
    elif output_format == OutputFormat.JSON:
        output = json.dumps(artifact, indent=2, sort_keys=True) + "\n"
    else:
        output = json.dumps(_patroni_document(conf), indent=2, sort_keys=True) + "\n"

    _write_output(output, args.output_file_name)
    return PGConfiguratorResult(
        result_code=ResultCode.DONE,
        result_data=conf,
        artifact=artifact,
        advisories=list(pgc.last_advisories),
    )


def _render_postgresql_conf(config, artifact):
    lines = [
        f"# Generated by pg-configurator {__version__}",
        "# PostgreSQL {}; host {}".format(
            artifact["inputs"]["pg_version"],
            socket.gethostname(),
        ),
    ]
    for item in artifact["advisories"]:
        lines.append("# {}: {}".format(item["severity"].upper(), item["message"]))
    lines.append("")
    lines.extend("{} = {}".format(*item) for item in config.items())
    return "\n".join(lines) + "\n"


def _patroni_document(config):
    parameters = {key: value.strip("'") for key, value in config.items()}
    parameters["max_replication_slots"] = str(max(4, int(parameters["max_replication_slots"])))
    return {"postgresql": {"parameters": parameters}}


def _write_output(output, output_file_name):
    if not output_file_name:
        print(output, end="")
        return
    if os.path.isdir(output_file_name):
        raise ValueError("output_file_name points to a directory")

    if os.path.exists(output_file_name):
        timestamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S%f")
        os.rename(output_file_name, f"{output_file_name}.{timestamp}.bak")
    with open(output_file_name, "w", encoding="utf-8") as file_handle:
        file_handle.write(output)
