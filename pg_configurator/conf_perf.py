"""Versioned PostgreSQL performance rules.

The expensive decisions are calculated once in :meth:`PGConfigurator.make_conf`.
Rules in this module map those independent budgets to version-specific GUCs;
they deliberately do not use the legacy composite ``system_score``.
"""

perf_alg_set = {
    "9.6": [
        # Autovacuum: conservative global defaults. Table-specific tuning belongs
        # in ALTER TABLE storage parameters informed by observed table churn.
        {"name": "autovacuum", "const": "on"},
        {"name": "autovacuum_max_workers", "alg": "autovacuum_workers", "to_unit": "as_is"},
        {"name": "autovacuum_work_mem", "alg": "autovacuum_work_mem_bytes"},
        {"name": "autovacuum_naptime", "alg": "autovacuum_naptime", "to_unit": "as_is"},
        {"name": "autovacuum_vacuum_threshold", "const": "50"},
        {"name": "autovacuum_analyze_threshold", "const": "50"},
        {
            "name": "autovacuum_vacuum_scale_factor",
            "alg": "autovacuum_vacuum_scale_factor",
            "to_unit": "as_is",
        },
        {
            "name": "autovacuum_analyze_scale_factor",
            "alg": "autovacuum_analyze_scale_factor",
            "to_unit": "as_is",
        },
        {
            "name": "autovacuum_vacuum_cost_limit",
            "alg": "autovacuum_cost_limit",
            "to_unit": "as_is",
        },
        {"name": "vacuum_cost_limit", "alg": "autovacuum_cost_limit", "to_unit": "as_is"},
        {
            "name": "autovacuum_vacuum_cost_delay",
            "alg": "autovacuum_cost_delay_ms",
            "unit_postfix": "ms",
        },
        {"name": "vacuum_cost_delay", "alg": "autovacuum_cost_delay_ms", "unit_postfix": "ms"},
        {"name": "autovacuum_freeze_max_age", "const": "200000000"},
        {"name": "autovacuum_multixact_freeze_max_age", "const": "400000000"},
        # Memory and connections.
        {"name": "shared_buffers", "alg": "shared_buffers_bytes"},
        {"name": "max_connections", "alg": "max_connections_value", "to_unit": "as_is"},
        {"name": "max_files_per_process", "const": "1000"},
        {
            "name": "superuser_reserved_connections",
            "alg": "max(3, min(10, int(connection_lock_target * 0.03)))",
            "to_unit": "as_is",
        },
        {"name": "work_mem", "alg": "calc_client_mem_values(max_connections_value, 0.1)[0]"},
        {"name": "temp_buffers", "alg": "calc_client_mem_values(max_connections_value, 0.1)[1]"},
        {"name": "maintenance_work_mem", "alg": "maintenance_work_mem_bytes"},
        # Authentication and dead-connection handling. Address binding, HBA,
        # certificates, and firewall policy remain deployment inputs.
        {"name": "password_encryption", "const": "on"},
        {
            "name": "authentication_timeout",
            "alg": "authentication_timeout",
            "to_unit": "as_is",
        },
        {
            "name": "tcp_keepalives_idle",
            "alg": "tcp_keepalives_idle_seconds",
            "unit_postfix": "s",
        },
        {
            "name": "tcp_keepalives_interval",
            "alg": "tcp_keepalives_interval_seconds",
            "unit_postfix": "s",
        },
        {
            "name": "tcp_keepalives_count",
            "alg": "tcp_keepalives_count",
            "to_unit": "as_is",
        },
        # WAL and durability. Safe crash recovery is never workload-dependent.
        {"name": "wal_level", "alg": "wal_level", "to_unit": "as_is"},
        {"name": "fsync", "const": "on"},
        {"name": "full_page_writes", "const": "on"},
        {"name": "synchronous_commit", "alg": "synchronous_commit", "to_unit": "as_is"},
        {
            "name": "synchronous_standby_names",
            "alg": "synchronous_standby_names",
            "to_unit": "quote",
        },
        {"name": "wal_compression", "const": "on"},
        {"name": "wal_buffers", "const": "-1"},
        {"name": "wal_writer_delay", "const": "200ms"},
        {"name": "wal_writer_flush_after", "const": "1MB"},
        {"name": "min_wal_size", "alg": "min_wal_size_bytes"},
        {"name": "max_wal_size", "alg": "max_wal_size_bytes"},
        {"name": "wal_keep_segments", "alg": "wal_keep_segments", "to_unit": "as_is"},
        # Replication topology.
        {"name": "max_replication_slots", "alg": "replication_slot_budget", "to_unit": "as_is"},
        {"name": "max_wal_senders", "alg": "wal_sender_budget", "to_unit": "as_is"},
        {
            "name": "wal_sender_timeout",
            "alg": "replication_network_timeout if replication_enabled else '0'",
            "to_unit": "as_is",
        },
        {
            "name": "wal_log_hints",
            "alg": "'on' if pitr_enabled or replication_enabled else 'off'",
            "to_unit": "as_is",
        },
        {"name": "hot_standby", "const": "on"},
        {
            "name": "wal_receiver_timeout",
            "alg": "replication_network_timeout if replication_enabled else '0'",
            "to_unit": "as_is",
        },
        {
            "name": "max_standby_streaming_delay",
            "alg": "max_standby_streaming_delay",
            "to_unit": "as_is",
        },
        {
            "name": "wal_receiver_status_interval",
            "alg": "'10s' if replication_enabled else '0'",
            "to_unit": "as_is",
        },
        {"name": "hot_standby_feedback", "const": "off"},
        # Checkpoints and group commit. commit_delay requires measurements and is
        # therefore left disabled instead of being inferred from host size.
        {"name": "checkpoint_timeout", "alg": "checkpoint_timeout", "to_unit": "as_is"},
        {"name": "checkpoint_completion_target", "const": "0.9"},
        {"name": "commit_delay", "const": "0"},
        {"name": "commit_siblings", "const": "5"},
        # Background writer: close to upstream defaults, avoiding extra write amplification.
        {"name": "bgwriter_delay", "const": "200ms"},
        {"name": "bgwriter_lru_maxpages", "const": "100"},
        {"name": "bgwriter_lru_multiplier", "const": "2.0"},
        # Planner and I/O use the same measured/fallback storage score.
        {"name": "effective_cache_size", "alg": "effective_cache_size_bytes"},
        {
            "name": "default_statistics_target",
            "alg": "default_statistics_target",
            "to_unit": "as_is",
        },
        {"name": "random_page_cost", "alg": "random_page_cost", "to_unit": "as_is"},
        {"name": "seq_page_cost", "const": "1"},
        {"name": "effective_io_concurrency", "alg": "effective_io_concurrency", "to_unit": "as_is"},
        {"name": "parallel_setup_cost", "alg": "parallel_setup_cost", "to_unit": "as_is"},
        {"name": "parallel_tuple_cost", "alg": "parallel_tuple_cost", "to_unit": "as_is"},
        {
            "name": "min_parallel_relation_size",
            "alg": "min_parallel_table_scan_size",
            "to_unit": "as_is",
        },
        {"name": "max_worker_processes", "alg": "worker_process_budget", "to_unit": "as_is"},
        {
            "name": "max_parallel_workers_per_gather",
            "alg": "parallel_workers_per_gather",
            "to_unit": "as_is",
        },
        # Lock memory follows connection capacity, not disk or RAM products.
        {
            "name": "max_locks_per_transaction",
            "alg": "max_locks_per_transaction",
            "to_unit": "as_is",
        },
        {
            "name": "max_pred_locks_per_transaction",
            "alg": "max_pred_locks_per_transaction",
            "to_unit": "as_is",
        },
        {"name": "lock_timeout", "alg": "lock_timeout", "to_unit": "as_is"},
        {"name": "deadlock_timeout", "alg": "deadlock_timeout", "to_unit": "as_is"},
        {"name": "statement_timeout", "alg": "statement_timeout", "to_unit": "as_is"},
        {
            "name": "idle_in_transaction_session_timeout",
            "alg": "idle_in_transaction_session_timeout",
            "to_unit": "as_is",
        },
        {
            "name": "stats_temp_directory",
            # Keep the cluster-relative upstream location. An absolute runtime
            # path is a pg_stand deployment decision and may not exist or have
            # suitable ownership on an arbitrary target.
            "const": "'pg_stat_tmp'",
        },
    ],
    "10": [
        {"__parent": "9.6"},
        {"name": "min_parallel_relation_size", "alg": "deprecated"},
        {
            "name": "min_parallel_table_scan_size",
            "alg": "min_parallel_table_scan_size",
            "to_unit": "as_is",
        },
        {
            "name": "min_parallel_index_scan_size",
            "alg": "min_parallel_index_scan_size",
            "to_unit": "as_is",
        },
        {"name": "password_encryption", "const": "scram-sha-256"},
        {"name": "max_parallel_workers", "alg": "parallel_worker_budget", "to_unit": "as_is"},
        {
            "name": "max_logical_replication_workers",
            "alg": "logical_worker_budget",
            "to_unit": "as_is",
        },
        {
            "name": "max_sync_workers_per_subscription",
            "alg": "sync_workers_per_subscription",
            "to_unit": "as_is",
        },
        {
            "name": "max_pred_locks_per_page",
            "alg": "max_pred_locks_per_page",
            "to_unit": "as_is",
        },
        {
            "name": "max_pred_locks_per_relation",
            "alg": "max_pred_locks_per_relation",
            "to_unit": "as_is",
        },
    ],
    "11": [
        {"__parent": "10"},
        {"name": "jit", "alg": "jit", "to_unit": "as_is"},
        {"name": "jit_above_cost", "alg": "jit_above_cost", "to_unit": "as_is"},
        {
            "name": "jit_inline_above_cost",
            "alg": "jit_inline_above_cost",
            "to_unit": "as_is",
        },
        {
            "name": "jit_optimize_above_cost",
            "alg": "jit_optimize_above_cost",
            "to_unit": "as_is",
        },
        {
            "name": "max_parallel_maintenance_workers",
            "alg": "parallel_maintenance_workers",
            "to_unit": "as_is",
        },
    ],
    "12": [
        {"__parent": "11"},
        {"name": "tcp_user_timeout", "alg": "tcp_user_timeout", "to_unit": "as_is"},
        {"name": "ssl_min_protocol_version", "const": "TLSv1.2"},
    ],
    "13": [
        {"__parent": "12"},
        {"name": "wal_keep_segments", "alg": "deprecated"},
        {"name": "autovacuum_vacuum_insert_threshold", "const": "50"},
        {"name": "autovacuum_vacuum_insert_scale_factor", "const": "0.02"},
        {"name": "logical_decoding_work_mem", "alg": "logical_decoding_work_mem_bytes"},
        {
            "name": "maintenance_io_concurrency",
            "alg": "maintenance_io_concurrency",
            "to_unit": "as_is",
        },
        {"name": "wal_keep_size", "alg": "wal_keep_bytes", "to_unit": "MB"},
        {"name": "max_slot_wal_keep_size", "alg": "max_slot_wal_keep_size_bytes", "to_unit": "MB"},
        {"name": "hash_mem_multiplier", "alg": "hash_mem_multiplier", "to_unit": "as_is"},
    ],
    "14": [
        {"__parent": "13"},
        {
            "name": "client_connection_check_interval",
            "alg": "client_connection_check_interval",
            "to_unit": "as_is",
        },
        {"name": "default_toast_compression", "const": "pglz"},
        {"name": "idle_session_timeout", "alg": "idle_session_timeout", "to_unit": "as_is"},
    ],
    "15": [
        {"__parent": "14"},
        {"name": "stats_temp_directory", "alg": "deprecated"},
        {"name": "wal_compression", "const": "pglz"},
    ],
    "16": [
        {"__parent": "15"},
        {"name": "vacuum_buffer_usage_limit", "alg": "vacuum_buffer_usage_limit_bytes"},
        {
            "name": "reserved_connections",
            "alg": "max(1, min(5, int(connection_lock_target * 0.02)))",
            "to_unit": "as_is",
        },
        {
            "name": "max_parallel_apply_workers_per_subscription",
            "alg": "parallel_apply_workers",
            "to_unit": "as_is",
        },
    ],
    "17": [
        {"__parent": "16"},
        {"name": "io_combine_limit", "alg": "io_combine_limit_bytes"},
        {"name": "transaction_timeout", "alg": "transaction_timeout", "to_unit": "as_is"},
    ],
    "18": [
        {"__parent": "17"},
        {"name": "io_max_combine_limit", "alg": "io_max_combine_limit_bytes"},
        {"name": "autovacuum_worker_slots", "alg": "autovacuum_worker_slots", "to_unit": "as_is"},
        {"name": "autovacuum_vacuum_max_threshold", "const": "100000"},
        {"name": "io_method", "const": "worker"},
        {"name": "io_workers", "alg": "io_workers", "to_unit": "as_is"},
        {"name": "io_max_concurrency", "const": "-1"},
        {
            "name": "idle_replication_slot_timeout",
            "alg": "'1d' if replication_slot_budget else '0'",
            "to_unit": "as_is",
        },
    ],
}
