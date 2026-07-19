alg_set_1c = {
    "9.6": [
        {"name": "autovacuum_naptime", "const": "20s"},
        {"name": "autovacuum_vacuum_cost_delay", "const": "2ms"},
        {"name": "from_collapse_limit", "const": "20"},
        {"name": "join_collapse_limit", "const": "20"},
        # ----------------------------------------------------------------------------------
        # Version and platform compatibility
        # ----------------------------------------------------------------------------------
        {"name": "escape_string_warning", "const": "off"},
        {"name": "standard_conforming_strings", "const": "off"},
        # ----------------------------------------------------------------------------------
        # Resource Consumption
        {"name": "max_connections", "alg": "calc_connection_limit(1000, min_conns)"},
        {
            "name": "max_files_per_process",
            # The official 1C baseline is 8000. pg_stand must verify that the
            # service's RLIMIT_NOFILE is higher before applying the artifact.
            "const": "8000",
        },
        {"name": "work_mem", "alg": "calc_client_mem_values(max_connections, 0.5)[0]"},
        {
            "name": "temp_buffers",
            "alg": "calc_client_mem_values(max_connections, 0.5)[1]",
            # where: if 1C then temp_buffers per session 50% of work_mem
        },
        # ----------------------------------------------------------------------------------
        # Checkpointer
        {"name": "checkpoint_timeout", "const": "15min"},
        {"name": "commit_delay", "const": "0"},
        # ----------------------------------------------------------------------------------
        # Background Writer
        {"name": "bgwriter_delay", "const": "20ms"},
        {
            "name": "bgwriter_lru_multiplier",  # some cushion against spikes in demand
            "const": "4",
        },
        {"name": "bgwriter_lru_maxpages", "const": "400"},
        # ----------------------------------------------------------------------------------
        # Query Planning
        {"name": "cpu_operator_cost", "const": "0.001"},
        {
            "name": "default_statistics_target",
            "alg": "profile_1c_statistics_target",
            "to_unit": "as_is",
        },
        {"name": "enable_mergejoin", "const": "off"},
        {"name": "geqo", "const": "on"},
        {"name": "geqo_threshold", "const": "12"},
        # ----------------------------------------------------------------------------------
        # Asynchronous Behavior
        {"name": "max_parallel_workers_per_gather", "const": "0"},
        # ----------------------------------------------------------------------------------
        # Connection and authentication
        # ----------------------------------------------------------------------------------
        {"name": "row_security", "const": "off"},
        {"name": "ssl", "const": "off"},
    ],
    "10": [
        {"__parent": "9.6"},
        {"name": "max_parallel_workers", "alg": "parallel_worker_budget", "to_unit": "as_is"},
    ],
    "11": [{"__parent": "10"}, {"name": "jit", "const": "off"}],
    "12": [{"__parent": "11"}],
    "13": [{"__parent": "12"}],
    "14": [{"__parent": "13"}],
    "15": [{"__parent": "14"}],
    "16": [{"__parent": "15"}],
    "17": [{"__parent": "16"}],
    "18": [{"__parent": "17"}],
}
