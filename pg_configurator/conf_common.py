"""Version-aware logging, statistics, and observability extension rules.

This module is the single source of truth for both extension dependencies and
the GUCs emitted for them. PostgreSQL contrib modules are validated against
the bundled ``pg_settings`` snapshot for every major. External extension
availability is a caller assertion until a deployment component performs a
live target preflight.
"""

SUPPORTED_PG_MAJORS = ("9.6", "10", "11", "12", "13", "14", "15", "16", "17", "18")

EXTENSION_SPECS = {
    "auto_explain": {
        "provider": "postgresql-contrib",
        "supported_versions": SUPPORTED_PG_MAJORS,
        "settings_validation": "pg_settings_snapshot",
    },
    "pg_stat_statements": {
        "provider": "postgresql-contrib",
        "supported_versions": SUPPORTED_PG_MAJORS,
        "settings_validation": "pg_settings_snapshot",
    },
    # The following modules are not guaranteed to be present in a vanilla
    # PostgreSQL installation. Their tuples describe the versions for which
    # pg-configurator has rules, not a promise that a binary package exists.
    "online_analyze": {
        "provider": "external",
        "supported_versions": SUPPORTED_PG_MAJORS,
        "settings_validation": "caller_inventory",
    },
    "pg_store_plans": {
        "provider": "external",
        "supported_versions": SUPPORTED_PG_MAJORS,
        "settings_validation": "caller_inventory",
    },
    "plantuner": {
        "provider": "external",
        "supported_versions": SUPPORTED_PG_MAJORS,
        "settings_validation": "caller_inventory",
    },
}

MANDATORY_COMMON_EXTENSIONS = frozenset({"auto_explain", "pg_stat_statements"})

PROFILE_EXTENSION_DEPENDENCIES = {
    "profile_1c": frozenset(
        {
            "auto_explain",
            "online_analyze",
            "pg_stat_statements",
            "pg_store_plans",
            "plantuner",
        }
    ),
    "ext_perf": frozenset(),
    "profile_backend_common": frozenset(
        {
            "auto_explain",
            "online_analyze",
            "pg_stat_statements",
            "pg_store_plans",
        }
    ),
    "profile_backend_perf": frozenset(),
}

EXTENSION_PRELOAD_ORDER = (
    "pg_stat_statements",
    "auto_explain",
    "pg_store_plans",
    "online_analyze",
    "plantuner",
)

common_alg_set = {
    "9.6": [
        # Extension preload is assembled from the selected profiles so common
        # rules cannot accidentally remove profile-specific libraries.
        {
            "name": "shared_preload_libraries",
            "alg": "shared_preload_libraries_value",
            "to_unit": "quote",
        },
        # auto_explain settings available in PostgreSQL 9.6 and newer.
        {
            "name": "auto_explain.log_min_duration",
            "alg": "auto_explain_log_min_duration",
            "to_unit": "as_is",
        },
        {"name": "auto_explain.log_analyze", "const": "on"},
        {"name": "auto_explain.log_verbose", "const": "off"},
        {"name": "auto_explain.log_buffers", "const": "on"},
        {"name": "auto_explain.log_format", "const": "text"},
        {"name": "auto_explain.log_nested_statements", "const": "off"},
        {"name": "auto_explain.log_timing", "const": "off"},
        {"name": "auto_explain.log_triggers", "const": "off"},
        {
            "name": "auto_explain.sample_rate",
            "alg": "auto_explain_sample_rate",
            "to_unit": "as_is",
        },
        # pg_stat_statements settings available in PostgreSQL 9.6 and newer.
        {"name": "pg_stat_statements.max", "const": "5000"},
        {"name": "pg_stat_statements.track", "const": "top"},
        {"name": "pg_stat_statements.save", "const": "on"},
        {"name": "pg_stat_statements.track_utility", "const": "on"},
        # Bounded CSV logging baseline.
        {"name": "logging_collector", "const": "on"},
        {"name": "log_destination", "const": "'csvlog'"},
        {"name": "log_directory", "const": "'pg_log'"},
        {"name": "log_filename", "const": "'postgresql-%Y-%m-%d_%H%M%S.log'"},
        {"name": "log_file_mode", "const": "384"},  # decimal representation of 0600
        {"name": "log_truncate_on_rotation", "const": "on"},
        {"name": "log_rotation_age", "const": "1d"},
        {"name": "log_rotation_size", "const": "256MB"},
        {"name": "log_min_messages", "const": "warning"},
        {"name": "log_min_error_statement", "const": "error"},
        {
            "name": "log_min_duration_statement",
            "alg": "log_min_duration_statement",
            "to_unit": "as_is",
        },
        {"name": "log_duration", "const": "off"},
        {"name": "log_statement", "const": "'ddl'"},
        {"name": "log_connections", "const": "off"},
        {"name": "log_disconnections", "const": "off"},
        {"name": "log_hostname", "const": "off"},
        {"name": "log_lock_waits", "const": "on"},
        {"name": "log_temp_files", "const": "10MB"},
        {"name": "log_checkpoints", "const": "on"},
        {"name": "log_autovacuum_min_duration", "const": "5s"},
        {
            "name": "log_line_prefix",
            "const": "'%m [%p] db=%d,user=%u,app=%a,client=%r '",
        },
        # Core statistics collection.
        {"name": "track_activities", "const": "on"},
        {"name": "track_counts", "const": "on"},
        {"name": "track_io_timing", "const": "on"},
        {"name": "track_functions", "const": "pl"},
        {"name": "track_activity_query_size", "const": "4096"},
    ],
    "10": [{"__parent": "9.6"}],
    "11": [{"__parent": "10"}],
    "12": [
        {"__parent": "11"},
        {"name": "auto_explain.log_level", "const": "log"},
        {"name": "auto_explain.log_settings", "const": "off"},
        {
            "name": "log_transaction_sample_rate",
            "alg": "log_transaction_sample_rate",
            "to_unit": "as_is",
        },
    ],
    "13": [
        {"__parent": "12"},
        {"name": "auto_explain.log_wal", "const": "on"},
        {"name": "pg_stat_statements.track_planning", "const": "off"},
        {"name": "log_min_duration_sample", "const": "1s"},
        {
            "name": "log_statement_sample_rate",
            "alg": "log_statement_sample_rate",
            "to_unit": "as_is",
        },
        # Avoid putting bind values, credentials, or personal data into logs.
        {"name": "log_parameter_max_length", "const": "0"},
        {"name": "log_parameter_max_length_on_error", "const": "0"},
    ],
    "14": [
        {"__parent": "13"},
        {"name": "compute_query_id", "const": "auto"},
        {"name": "track_wal_io_timing", "const": "on"},
        {
            "name": "log_recovery_conflict_waits",
            "alg": "'on' if replication_enabled else 'off'",
            "to_unit": "as_is",
        },
    ],
    "15": [{"__parent": "14"}],
    "16": [
        {"__parent": "15"},
        {"name": "auto_explain.log_parameter_max_length", "const": "0"},
    ],
    "17": [{"__parent": "16"}],
    "18": [{"__parent": "17"}],
}


# Profile-specific extension GUCs live here as well. They are appended only
# when the corresponding workload profile is selected, and inherit according
# to the extension settings actually present in each PostgreSQL major.
common_profile_alg_sets = {
    "profile_1c": {
        "9.6": [
            {"name": "auto_explain.log_min_duration", "const": "5s"},
            {"name": "online_analyze.enable", "const": "off"},
            {"name": "online_analyze.verbose", "const": "off"},
            {"name": "online_analyze.scale_factor", "const": "0.1"},
            {"name": "online_analyze.threshold", "const": "50"},
            {"name": "online_analyze.local_tracking", "const": "on"},
            {"name": "online_analyze.min_interval", "const": "10000"},
            {"name": "online_analyze.table_type", "const": "temporary"},
            {"name": "pg_store_plans.max", "const": "15000"},
            {"name": "pg_store_plans.track", "const": "top"},
            {"name": "pg_store_plans.max_plan_length", "const": "15000"},
            {"name": "pg_store_plans.plan_format", "const": "raw"},
            {"name": "pg_store_plans.min_duration", "const": "3000"},
            {"name": "pg_store_plans.log_analyze", "const": "on"},
            {"name": "pg_store_plans.log_buffers", "const": "on"},
            {"name": "plantuner.fix_empty_table", "const": "on"},
        ],
        "10": [{"__parent": "9.6"}],
        "11": [{"__parent": "10"}],
        "12": [{"__parent": "11"}],
        "13": [{"__parent": "12"}],
        "14": [{"__parent": "13"}],
        "15": [{"__parent": "14"}],
        "16": [{"__parent": "15"}],
        "17": [{"__parent": "16"}],
        "18": [{"__parent": "17"}],
    },
    "profile_backend_common": {
        "9.6": [
            {"name": "online_analyze.enable", "const": "on"},
            {"name": "online_analyze.verbose", "const": "off"},
            {"name": "online_analyze.scale_factor", "const": "0.01"},
            {"name": "online_analyze.threshold", "const": "500"},
            {"name": "online_analyze.local_tracking", "const": "on"},
            {"name": "online_analyze.min_interval", "const": "10000"},
            {"name": "online_analyze.table_type", "const": "temporary"},
            {"name": "pg_store_plans.max", "const": "10000"},
            {"name": "pg_store_plans.track", "const": "top"},
            {"name": "pg_store_plans.max_plan_length", "const": "5000"},
            {"name": "pg_store_plans.plan_format", "const": "raw"},
            {"name": "pg_store_plans.min_duration", "const": "300"},
            {"name": "pg_store_plans.log_analyze", "const": "on"},
            {"name": "pg_store_plans.log_buffers", "const": "on"},
        ],
        "10": [{"__parent": "9.6"}],
        "11": [{"__parent": "10"}],
        "12": [{"__parent": "11"}],
        "13": [{"__parent": "12"}],
        "14": [{"__parent": "13"}],
        "15": [{"__parent": "14"}],
        "16": [{"__parent": "15"}],
        "17": [{"__parent": "16"}],
        "18": [{"__parent": "17"}],
    },
}
