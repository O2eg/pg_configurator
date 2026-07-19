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
from pg_configurator.conf_common import common_alg_set
from pg_configurator.conf_perf import perf_alg_set
from pg_configurator.conf_profiles import (
    alg_set_1c,
    backend_common_alg_set,
    backend_perf_alg_set,
    ext_alg_set,
)
from pg_configurator.rule_engine import RuleEvaluationError, RuleEvaluator
from pg_configurator.version import __version__


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
    STATISTIC = "statistic"  # Low reliability, fast speed, long recovery
    # Purely analytical and large aggregations
    # Transactions may be lost in case of a crash
    MIXED = "mixed"  # Medium reliability, medium speed, medium recovery
    # Mostly complicated real time SQL queries
    FINANCIAL = "financial"  # High reliability, low speed, fast recovery
    # Billing tasks. Can't lose transactions in case of a crash


class DiskType(BasicEnum, Enum):
    # We assume that we have minimum 2 disk in hardware RAID1 (or 4 in RAID10) with BBU
    SATA = "SATA"
    SAS = "SAS"
    SSD = "SSD"
    NVME = "NVME"
    NETWORK = "NETWORK"


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

    profile_extensions = {
        "profile_1c": {
            "auto_explain",
            "online_analyze",
            "pg_stat_statements",
            "pg_store_plans",
            "plantuner",
        },
        "ext_perf": set(),
        "profile_backend_common": {
            "auto_explain",
            "online_analyze",
            "pg_stat_statements",
            "pg_store_plans",
        },
        "profile_backend_perf": set(),
    }

    restart_required_settings = {
        "autovacuum_freeze_max_age",
        "autovacuum_multixact_freeze_max_age",
        "autovacuum_worker_slots",
        "huge_pages",
        "io_max_concurrency",
        "io_method",
        "io_workers",
        "max_connections",
        "max_locks_per_transaction",
        "max_pred_locks_per_transaction",
        "max_replication_slots",
        "max_wal_senders",
        "max_worker_processes",
        "shared_buffers",
        "shared_preload_libraries",
        "wal_buffers",
        "wal_level",
    }

    current_dir = os.path.dirname(os.path.realpath(__file__))
    output_dir = os.getcwd()
    args = {}
    ext_params = {}

    def __init__(self, args, ext_params):
        self.args = args
        self.ext_params = ext_params
        self.last_artifact = None
        self.last_calculation = {}
        self.last_inputs = {}
        self.last_overrides = []
        self.last_parameter_details = {}
        self.last_warnings = []
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
    def calc_synchronous_commit(duty_db, replication_enabled):
        if replication_enabled:
            if duty_db == DutyDB.STATISTIC:
                return "off"
            if duty_db == DutyDB.MIXED:
                return "local"
            if duty_db == DutyDB.FINANCIAL:
                return "remote_apply"
        else:
            if duty_db == DutyDB.STATISTIC:
                return "off"
            if duty_db == DutyDB.MIXED:
                return "off"
            if duty_db == DutyDB.FINANCIAL:
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

    @staticmethod
    def _validate_positive_range(min_value, max_value, min_name, max_name):
        for name, value in ((min_name, min_value), (max_name, max_value)):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if min_value > max_value:
            raise ValueError(f"{min_name} must not be greater than {max_name}")

    @classmethod
    def _load_supported_setting_names(cls, pg_version):
        settings_file = os.path.join(
            cls.current_dir, "pg_settings_history", cls.known_versions[pg_version]
        )
        with open(settings_file, encoding="utf-8") as file_handle:
            reader = csv.reader(file_handle)
            next(reader, None)
            return {row[0] for row in reader if row}

    @classmethod
    def _validate_config_parameters(cls, config, pg_version):
        supported_settings = cls._load_supported_setting_names(pg_version)
        unknown_settings = sorted(
            setting_name
            for setting_name in config
            if "." not in setting_name and setting_name not in supported_settings
        )
        if unknown_settings:
            raise ValueError(
                "Parameters are not supported by PostgreSQL {}: {}".format(
                    pg_version, ", ".join(unknown_settings)
                )
            )

    def make_conf(
        self,
        cpu_cores,
        ram_value,
        disk_type=DiskType.SAS,
        duty_db=DutyDB.MIXED,
        replication_enabled=True,
        pg_version="15",
        reserved_ram_percent=10,  # for calc of total_ram_in_bytes
        reserved_system_ram="256Mi",  # for calc of total_ram_in_bytes
        shared_buffers_part=0.7,
        client_mem_part=0.2,  # for all available connections
        maintenance_mem_part=0.1,  # memory for maintenance connections + autovacuum workers
        autovacuum_workers_mem_part=0.5,  # from maintenance_mem_part
        maintenance_conns_mem_part=0.5,  # from maintenance_mem_part
        min_conns=50,
        max_conns=500,
        min_autovac_workers=4,  # autovacuum workers
        max_autovac_workers=20,
        min_maint_conns=4,  # maintenance connections
        max_maint_conns=16,
        platform=Platform.LINUX,
        common_conf=False,
        conf_profiles=None,
        disk_score=None,
        work_mem_concurrency_factor=2.0,
        available_extensions=None,
    ):
        # Validate public inputs before calculating a configuration.
        # checks
        pg_version = str(pg_version)
        if pg_version not in self.known_versions:
            raise ValueError(f"Unsupported PostgreSQL version: {pg_version}")

        disk_type = self._coerce_enum(disk_type, DiskType, "disk_type")
        duty_db = self._coerce_enum(duty_db, DutyDB, "duty_db")
        platform = self._coerce_enum(platform, Platform, "platform")

        if not isinstance(replication_enabled, bool):
            raise ValueError("replication_enabled must be a boolean")
        if not isinstance(common_conf, bool):
            raise ValueError("common_conf must be a boolean")
        if conf_profiles is not None and not isinstance(conf_profiles, str):
            raise ValueError("conf_profiles must be a comma-separated string")
        if available_extensions is not None and not isinstance(
            available_extensions, (str, list, tuple, set)
        ):
            raise ValueError("available_extensions must be a comma-separated string or collection")

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

        self._validate_fraction_group(
            {
                "shared_buffers_part": shared_buffers_part,
                "client_mem_part": client_mem_part,
                "maintenance_mem_part": maintenance_mem_part,
            },
            "Main memory parts",
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
        self._validate_positive_range(
            min_maint_conns, max_maint_conns, "min_maint_conns", "max_maint_conns"
        )
        # Fixed normalization boundaries.
        # consts
        page_size = 8192
        max_cpu_cores = 96  # maximum of CPU cores in system, 4 CPU with 12 cores=>24 threads = 96
        min_cpu_cores = 4
        max_ram = "768Gi"
        min_work_mem_in_bytes = 64 * 1024
        min_temp_buffers_in_bytes = 100 * page_size
        # Pre-calculated values.
        # pre-calculated vars
        total_cpu_cores = UnitConverter.size_cpu_to_ncores(cpu_cores)
        if total_cpu_cores <= 0:
            raise ValueError("cpu_cores must be greater than 0")

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

        max_ram_in_bytes = (
            UnitConverter.size_from(max_ram, system=UnitConverter.sys_iec) * available_ram_ratio
            - reserved_system_ram_in_bytes
        )
        if max_ram_in_bytes <= 0:
            raise ValueError(f"reserved memory exceeds the RAM normalization limit {max_ram}")

        cpu_scale_ratio = min(
            max((total_cpu_cores - min_cpu_cores) / (max_cpu_cores - min_cpu_cores), 0), 1
        )
        connection_capacity = int(
            (total_ram_in_bytes * client_mem_part)
            / (min_work_mem_in_bytes + min_temp_buffers_in_bytes)
        )
        if connection_capacity < min_conns:
            raise ValueError(
                f"Client memory budget supports {connection_capacity} connections, "
                f"less than min_conns={min_conns}"
            )

        def calc_cpu_scale(v_min, v_max):
            return cpu_scale_ratio * (v_max - v_min) + v_min

        def calc_connection_scale(v_min, v_max):
            if connection_capacity < v_min:
                raise ValueError(
                    f"Client memory budget supports {connection_capacity} connections, "
                    f"less than the required minimum {v_min}"
                )
            return int(min(calc_cpu_scale(v_min, v_max), connection_capacity, v_max))

        def calc_connection_limit(desired_connections, required_minimum):
            if desired_connections < required_minimum:
                raise ValueError(
                    f"desired_connections={desired_connections} is less than the required "
                    f"minimum {required_minimum}"
                )
            if connection_capacity < required_minimum:
                raise ValueError(
                    f"Client memory budget supports {connection_capacity} connections, "
                    f"less than the required minimum {required_minimum}"
                )
            return int(min(desired_connections, connection_capacity))

        def calc_client_mem_values(connection_count, temp_buffers_part=0.1):
            if connection_count <= 0:
                raise ValueError("connection_count must be greater than 0")
            if temp_buffers_part <= 0 or temp_buffers_part >= 1:
                raise ValueError("temp_buffers_part must be greater than 0 and less than 1")

            memory_per_connection = (total_ram_in_bytes * client_mem_part) / connection_count
            minimum_required = min_work_mem_in_bytes + min_temp_buffers_in_bytes
            if memory_per_connection < minimum_required:
                raise ValueError(
                    "Client memory budget per connection is lower than PostgreSQL minimums"
                )

            temp_buffers_value = max(
                memory_per_connection * temp_buffers_part, min_temp_buffers_in_bytes
            )
            work_mem_value = (
                memory_per_connection - temp_buffers_value
            ) / work_mem_concurrency_factor
            if work_mem_value < min_work_mem_in_bytes:
                work_mem_value = min_work_mem_in_bytes
                temp_buffers_value = memory_per_connection - work_mem_value
            return work_mem_value, temp_buffers_value

        maint_max_conns = calc_cpu_scale(min_maint_conns, max_maint_conns)
        # System score calculation in percent.
        # system scores calculation in percents
        cpu_scores = min(max((total_cpu_cores * 100) / max_cpu_cores, 0), 100)
        ram_scores = min(max((total_ram_in_bytes * 100) / max_ram_in_bytes, 0), 100)
        default_disk_scores = {
            DiskType.SATA: 20,
            DiskType.SAS: 40,
            DiskType.SSD: 100,
            DiskType.NVME: 100,
            DiskType.NETWORK: 50,
        }
        disk_scores = disk_score if disk_score is not None else default_disk_scores[disk_type]

        system_scores = (
            0.5 * cpu_scores * ram_scores * 0.866
            + 0.5 * ram_scores * disk_scores * 0.866
            + 0.5 * disk_scores * cpu_scores * 0.866
        )
        # where triangle_surface = 0.5 * cpu_scores * ram_scores * sin(120)
        # sin(120) = 0.866

        system_scores_max = (
            0.5 * 100 * 100 * 0.866 + 0.5 * 100 * 100 * 0.866 + 0.5 * 100 * 100 * 0.866
        )

        system_scores = min(max((system_scores * 100) / system_scores_max, 0), 100)
        # 100 represents max_cpu_cores, max_ram, and reference SSD storage.

        def calc_system_scores_scale(v_min, v_max):
            return (system_scores / 100) * (v_max - v_min) + v_min

        def calc_disk_scale(v_min, v_max):
            return (disk_scores / 100) * (v_max - v_min) + v_min

        selected_profiles = []
        if conf_profiles:
            selected_profiles = [item.strip() for item in conf_profiles.split(",")]
            if any(profile == "" for profile in selected_profiles):
                raise ValueError("Profile name must not be empty")

        warnings = []
        if shared_buffers_part > 0.4:
            warnings.append(
                f"shared_buffers_part={shared_buffers_part} is an aggressive empirical setting; "
                "verify it against the workload and operating-system cache"
            )
        if disk_score is None:
            warnings.append(
                f"Storage score is inferred from disk_type={disk_type.value}; "
                "use disk_score with measured "
                "IOPS/latency and the complete data/WAL storage topology"
            )
        warnings.append(
            "work_mem assumes a combined concurrency factor of "
            f"{work_mem_concurrency_factor} for concurrent operators, "
            "parallel participants, and hash amplification"
        )

        if "profile_1c" in selected_profiles:
            warnings.extend(
                [
                    "profile_1c disables SSL",
                    "profile_1c disables row_security",
                    "profile_1c disables standard_conforming_strings",
                    "profile_1c requests up to 1000 connections and is capped by the memory budget",
                ]
            )

        required_extensions = set()
        for profile in selected_profiles:
            required_extensions.update(self.profile_extensions.get(profile, set()))
        if common_conf:
            required_extensions.update({"auto_explain", "pg_stat_statements"})

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

        if required_extensions:
            if normalized_available_extensions is None:
                warnings.append(
                    "Required extensions were not preflighted: {}".format(
                        ", ".join(sorted(required_extensions))
                    )
                )
            else:
                missing_extensions = sorted(required_extensions - normalized_available_extensions)
                if missing_extensions:
                    raise ValueError(
                        "Required extensions are unavailable: {}".format(
                            ", ".join(missing_extensions)
                        )
                    )

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
            "UnitConverter": UnitConverter,
            "autovacuum_workers_mem_part": autovacuum_workers_mem_part,
            "calc_client_mem_values": calc_client_mem_values,
            "calc_connection_limit": calc_connection_limit,
            "calc_connection_scale": calc_connection_scale,
            "calc_cpu_scale": calc_cpu_scale,
            "calc_disk_scale": calc_disk_scale,
            "calc_system_scores_scale": calc_system_scores_scale,
            "disk_scores": disk_scores,
            "disk_type": disk_type,
            "duty_db": duty_db,
            "float": float,
            "int": int,
            "maint_max_conns": maint_max_conns,
            "maintenance_conns_mem_part": maintenance_conns_mem_part,
            "maintenance_mem_part": maintenance_mem_part,
            "max": max,
            "max_autovac_workers": max_autovac_workers,
            "max_connections": None,
            "max_conns": max_conns,
            "min": min,
            "min_autovac_workers": min_autovac_workers,
            "min_conns": min_conns,
            "page_size": page_size,
            "platform": platform,
            "replication_enabled": replication_enabled,
            "round": round,
            "shared_buffers": None,
            "shared_buffers_part": shared_buffers_part,
            "total_cpu_cores": total_cpu_cores,
            "total_ram_in_bytes": total_ram_in_bytes,
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
                UnitConverter,
            },
        )

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
            if debug_enabled:
                print(f"Processing: {param_name} = {rule_expression}", file=sys.stderr)

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
            parameter_details[param_name] = {
                "value": formatted_value,
                "raw_value": raw_value,
                "source": source,
                "rule": rule_expression,
                "apply_mode": (
                    "restart" if param_name in self.restart_required_settings else "reload"
                ),
            }
            if param_name.isidentifier():
                rule_context[param_name] = raw_value

        config_res = dict(sorted(config_res.items()))
        self._validate_config_parameters(config_res, pg_version)
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
            "duty_db": duty_db.value,
            "pg_version": pg_version,
            "profiles": selected_profiles,
            "ram_bytes": ram_in_bytes,
            "replication_enabled": replication_enabled,
            "reserved_ram_percent": reserved_ram_percent,
            "reserved_system_ram_bytes": reserved_system_ram_in_bytes,
            "work_mem_concurrency_factor": work_mem_concurrency_factor,
        }
        self.last_calculation = {
            "available_ram_bytes": int(total_ram_in_bytes),
            "connection_capacity": connection_capacity,
            "cpu_score": round(cpu_scores, 4),
            "disk_score": round(disk_scores, 4),
            "ram_score": round(ram_scores, 4),
            "system_score": round(system_scores, 4),
        }
        self.last_parameter_details = dict(sorted(parameter_details.items()))
        self.last_overrides = overrides
        self.last_warnings = warnings
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
            "schema_version": "pg_configurator/v1",
            "kind": "PostgreSQLConfiguration",
            "generator": {
                "name": "pg-configurator",
                "version": __version__,
            },
            "generated_at": datetime.datetime.now(datetime.timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "inputs": self.last_inputs,
            "calculation": self.last_calculation,
            "parameters": self.last_parameter_details,
            "overrides": self.last_overrides,
            "warnings": self.last_warnings,
            "postgresql_conf": config,
        }
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
        parser.add_argument("--output-file-name", help="Save to file", type=str, default="")
        parser.add_argument(
            "--db-cpu",
            help="Available CPU cores, (default: %(default)s)",
            type=str,
            default=psutil.cpu_count(),
        )
        parser.add_argument(
            "--db-ram",
            help="Available RAM memory, (default: %(default)s)",
            type=str,
            default=UnitConverter.size_to(
                psutil.virtual_memory().total, system=UnitConverter.sys_iec
            ),
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
            help="Replication is enabled, (default: %(default)s)",
            type=parse_bool,
            default=mca["replication_enabled"],
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
            help="Memory part for all available connections, (default: %(default)s)",
            type=float,
            default=mca["client_mem_part"],
        )
        parser.add_argument(
            "--maintenance-mem-part",
            help="Memory part for maintenance connections, (default: %(default)s)",
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
                "Add common postgresql.conf settings such as statistics "
                "collector and logging options"
            ),
            action="store_true",
            default=False,
        )
        parser.add_argument(
            "--platform",
            help="Platform on which the DB is running, (default: %(default)s)",
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
            help="Comma-separated extensions available on the target PostgreSQL installation",
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


def run_pgc(external_args=None, ext_params=None) -> PGConfiguratorResult:
    parser = PGConfigurator.get_arg_parser()
    if external_args is None:
        args = parser.parse_args()
    elif isinstance(external_args, (list, tuple)):
        args = parser.parse_args(external_args)
    else:
        args = external_args

    pgc = PGConfigurator(args, ext_params)
    if args.debug:
        print(
            "{} pg-configurator started".format(datetime.datetime.now().isoformat(" ")),
            file=sys.stderr,
        )
        for argument_name, argument_value in vars(args).items():
            print(
                f"# {argument_name} = {argument_value}",
                file=sys.stderr,
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
        available_extensions=args.available_extensions,
    )
    artifact = pgc.build_artifact(conf)
    output_format = (
        args.output_format
        if isinstance(args.output_format, OutputFormat)
        else OutputFormat(args.output_format)
    )

    if output_format == OutputFormat.CONF:
        output = _render_postgresql_conf(conf, artifact)
    elif output_format == OutputFormat.JSON:
        output = json.dumps(artifact, indent=2, sort_keys=True) + "\n"
    else:
        patroni_conf = {
            "postgresql": {"parameters": {key: value.strip("'") for key, value in conf.items()}}
        }
        output = json.dumps(patroni_conf, indent=2, sort_keys=True) + "\n"

    _write_output(output, args.output_file_name)
    return PGConfiguratorResult(
        result_code=ResultCode.DONE,
        result_data=conf,
        artifact=artifact,
        warnings=list(pgc.last_warnings),
    )


def _render_postgresql_conf(config, artifact):
    lines = [
        f"# Generated by pg-configurator {__version__}",
        "# PostgreSQL {}; host {}".format(
            artifact["inputs"]["pg_version"],
            socket.gethostname(),
        ),
    ]
    for warning in artifact["warnings"]:
        lines.append(f"# WARNING: {warning}")
    lines.append("")
    lines.extend("{} = {}".format(*item) for item in config.items())
    return "\n".join(lines) + "\n"


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
