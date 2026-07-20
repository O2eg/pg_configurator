"""Write-intensive workload overrides that preserve global safety budgets."""

ext_alg_set = {
    "9.6": [
        {"name": "autovacuum_naptime", "const": "15s"},
        {"name": "autovacuum_vacuum_scale_factor", "const": "0.01"},
        {"name": "autovacuum_analyze_scale_factor", "const": "0.005"},
        {
            "name": "autovacuum_vacuum_cost_limit",
            "alg": "autovacuum_cost_limit",
            "to_unit": "as_is",
        },
        {"name": "vacuum_cost_limit", "alg": "autovacuum_cost_limit", "to_unit": "as_is"},
        {
            "name": "max_parallel_workers_per_gather",
            "alg": "parallel_workers_per_gather",
            "to_unit": "as_is",
        },
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
}
