"""Backend-specific overrides for the base performance rules.

Only settings that differ from ``conf_perf.perf_alg_set`` belong here. Version
inheritance is provided by the base rule set when the profile is applied.
"""

backend_perf_alg_set = {
    "9.6": [
        {"name": "autovacuum_naptime", "const": "'15s'"},
        {
            "name": "autovacuum_vacuum_cost_limit",
            "alg": "int(calc_system_scores_scale(2000, 8000))",
            "unit": "as_is",
        },
        {"name": "vacuum_cost_limit", "const": "8000"},
        {"name": "autovacuum_freeze_max_age", "const": "500000000"},
        {"name": "autovacuum_multixact_freeze_max_age", "const": "800000000"},
        {
            "name": "max_files_per_process",
            "alg": "int(calc_cpu_scale(1000, 10000))",
            "to_unit": "as_is",
        },
        {"name": "huge_pages", "const": "try"},
        {"name": "fsync", "const": "on"},
        {
            "name": "wal_buffers",
            "alg": """
                int(
                    calc_system_scores_scale(
                        UnitConverter.size_from('16MB', system=UnitConverter.sys_pg),
                        UnitConverter.size_from('256MB', system=UnitConverter.sys_pg)
                    )
                )
            """,
            "to_unit": "MB",
        },
        {
            "name": "min_wal_size",
            "alg": """
                calc_system_scores_scale(
                    UnitConverter.size_from('512MB', system=UnitConverter.sys_pg),
                    UnitConverter.size_from('16GB', system=UnitConverter.sys_pg)
                )
            """,
        },
        {
            "name": "max_wal_size",
            "alg": """
                calc_system_scores_scale(
                    UnitConverter.size_from('1GB', system=UnitConverter.sys_pg),
                    UnitConverter.size_from(
                        '32GB' if duty_db == DutyDB.FINANCIAL else '64GB',
                        system=UnitConverter.sys_pg
                    )
                )
            """,
        },
        {
            "name": "wal_sender_timeout",
            "alg": "'300s' if replication_enabled else '0'",
            "to_unit": "as_is",
        },
        {
            "name": "wal_keep_segments",
            "alg": "int(calc_system_scores_scale(128, 1024)) if replication_enabled else 0",
        },
        {
            "name": "wal_receiver_timeout",
            "alg": "'300s' if replication_enabled else '0'",
            "to_unit": "as_is",
        },
        {
            "name": "max_standby_streaming_delay",
            "alg": """
                '90s' if duty_db == DutyDB.FINANCIAL and replication_enabled else
                '1800s' if duty_db == DutyDB.MIXED else
                '-1'
            """,
            "to_unit": "as_is",
        },
        {
            "name": "checkpoint_timeout",
            "alg": """
                '15min' if duty_db == DutyDB.FINANCIAL else
                '30min' if duty_db == DutyDB.MIXED else
                '1h'
            """,
            "to_unit": "as_is",
        },
        {"name": "checkpoint_warning", "const": "30s"},
        {
            "name": "checkpoint_completion_target",
            "alg": """
                '0.8' if duty_db == DutyDB.FINANCIAL else
                '0.85' if duty_db == DutyDB.MIXED else
                '0.9'
            """,
            "to_unit": "as_is",
        },
        {
            "name": "bgwriter_delay",
            "alg": "int(calc_system_scores_scale(50, 200))",
            "unit_postfix": "ms",
        },
        {
            "name": "bgwriter_lru_maxpages",
            "alg": "int(calc_system_scores_scale(500, 1000))",
            "to_unit": "as_is",
        },
        {"name": "default_statistics_target", "const": "500"},
        {
            "name": "random_page_cost",
            "alg": """
                '4' if disk_type == DiskType.SATA else
                '2.5' if disk_type == DiskType.SAS else
                '3' if disk_type == DiskType.NETWORK else
                '1.1'
            """,
            "to_unit": "as_is",
        },
        {
            "name": "join_collapse_limit",
            "alg": """
                '8' if duty_db == DutyDB.FINANCIAL else
                '9' if duty_db == DutyDB.MIXED else
                '10'
            """,
            "to_unit": "as_is",
        },
        {
            "name": "from_collapse_limit",
            "alg": """
                '8' if duty_db == DutyDB.FINANCIAL else
                '9' if duty_db == DutyDB.MIXED else
                '10'
            """,
            "to_unit": "as_is",
        },
        {"name": "geqo", "const": "on"},
        {"name": "geqo_threshold", "const": "12"},
        {"name": "max_worker_processes", "alg": "calc_cpu_scale(4, 96)"},
        {
            "name": "max_parallel_workers_per_gather",
            "alg": """
                calc_cpu_scale(2, 4) if duty_db == DutyDB.FINANCIAL else
                calc_cpu_scale(2, 8) if duty_db == DutyDB.MIXED else
                calc_cpu_scale(2, 16)
            """,
        },
        {"name": "stats_temp_directory", "alg": "deprecated"},
    ],
    "10": [
        {
            "name": "max_parallel_workers",
            "alg": """
                calc_cpu_scale(4, 12) if duty_db == DutyDB.FINANCIAL else
                calc_cpu_scale(4, 24) if duty_db == DutyDB.MIXED else
                calc_cpu_scale(4, 32)
            """,
        },
        {
            "name": "max_logical_replication_workers",
            "alg": """
                calc_cpu_scale(4, 12) if duty_db == DutyDB.FINANCIAL else
                calc_cpu_scale(4, 16) if duty_db == DutyDB.MIXED else
                calc_cpu_scale(6, 24)
            """,
        },
        {
            "name": "max_sync_workers_per_subscription",
            "alg": """
                calc_cpu_scale(2, 8) if duty_db == DutyDB.FINANCIAL else
                calc_cpu_scale(2, 12) if duty_db == DutyDB.MIXED else
                calc_cpu_scale(4, 16)
            """,
        },
    ],
    "12": [{"name": "wal_keep_segments", "alg": "deprecated"}],
    "13": [
        {
            "name": "maintenance_io_concurrency",
            "alg": """
                '2' if disk_type == DiskType.SATA else
                '4' if disk_type == DiskType.SAS else
                '16' if disk_type == DiskType.NETWORK else
                '256' if disk_type == DiskType.NVME else
                '128'
            """,
            "to_unit": "as_is",
        },
        {
            "name": "wal_keep_size",
            "alg": """
                int(
                    calc_system_scores_scale(
                        UnitConverter.size_from('1024MB', system=UnitConverter.sys_pg),
                        UnitConverter.size_from('16384MB', system=UnitConverter.sys_pg)
                    )
                )
            """,
            "to_unit": "MB",
        },
        {
            "name": "hash_mem_multiplier",
            "alg": """
                '1.2' if duty_db == DutyDB.FINANCIAL else
                '2.0' if duty_db == DutyDB.MIXED else
                '8.0'
            """,
            "to_unit": "as_is",
        },
    ],
    "14": [
        {
            "name": "client_connection_check_interval",
            "alg": """
                '3s' if duty_db == DutyDB.FINANCIAL else
                '5s' if duty_db == DutyDB.MIXED else
                '30s'
            """,
            "to_unit": "as_is",
        },
        {
            "name": "default_toast_compression",
            "alg": "'pglz' if duty_db in (DutyDB.FINANCIAL, DutyDB.MIXED) else 'lz4'",
            "to_unit": "as_is",
        },
        {"name": "enable_async_append", "const": "on"},
    ],
    "15": [{"name": "wal_compression", "to_unit": "as_is", "const": "lz4"}],
    "17": [
        {
            "name": "subtransaction_buffers",
            "alg": """
                int(
                    calc_system_scores_scale(
                        UnitConverter.size_from('4MB', system=UnitConverter.sys_pg),
                        UnitConverter.size_from('1024MB', system=UnitConverter.sys_pg)
                    )
                )
            """,
            "to_unit": "MB",
        },
        {
            "name": "transaction_buffers",
            "alg": """
                int(
                    calc_system_scores_scale(
                        UnitConverter.size_from('4MB', system=UnitConverter.sys_pg),
                        UnitConverter.size_from('1024MB', system=UnitConverter.sys_pg)
                    )
                )
            """,
            "to_unit": "MB",
        },
    ],
}
