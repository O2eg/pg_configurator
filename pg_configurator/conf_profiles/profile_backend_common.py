"""Core safety/compatibility settings for backend workloads.

Logging and extension rules intentionally live in :mod:`conf_common`.
"""

backend_common_alg_set = {
    "9.6": [
        {"name": "escape_string_warning", "const": "on"},
        {"name": "standard_conforming_strings", "const": "on"},
        {"name": "row_security", "const": "on"},
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
