"""Backend workload overrides for the unified safety-first model."""

backend_perf_alg_set = {
    "9.6": [
        {"name": "autovacuum_naptime", "const": "15s"},
        {"name": "huge_pages", "const": "try"},
        {"name": "checkpoint_warning", "const": "30s"},
        {
            "name": "default_statistics_target",
            "alg": "profile_backend_statistics_target",
            "to_unit": "as_is",
        },
        {
            "name": "join_collapse_limit",
            "alg": (
                "8 if duty_db == DutyDB.FINANCIAL else "
                "8 if duty_db == DutyDB.OLTP else "
                "9 if duty_db == DutyDB.MIXED else 10"
            ),
            "to_unit": "as_is",
        },
        {
            "name": "from_collapse_limit",
            "alg": (
                "8 if duty_db == DutyDB.FINANCIAL else "
                "8 if duty_db == DutyDB.OLTP else "
                "9 if duty_db == DutyDB.MIXED else 10"
            ),
            "to_unit": "as_is",
        },
        {"name": "geqo", "const": "on"},
        {"name": "geqo_threshold", "const": "12"},
        {
            "name": "max_parallel_workers_per_gather",
            "alg": "parallel_workers_per_gather",
            "to_unit": "as_is",
        },
    ],
    "10": [],
    "11": [],
    "12": [],
    "13": [],
    "14": [
        {"name": "enable_async_append", "const": "on"},
    ],
    "15": [{"name": "wal_compression", "const": "pglz"}],
    "16": [],
    "17": [],
    "18": [],
}
