"""Stable machine contract used by pg_play adapters.

The human CLI remains the default.  This module contains only deterministic
serialization helpers and public contract metadata; it never applies a
configuration to a running service.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pg_configurator.version import __version__

CONTRACT_VERSION = "pg_play/component/v1"
CAPABILITY_SCHEMA_VERSION = "pg_play/capabilities/v1"
MACHINE_INTERFACE = {
    "machine_flag": "--machine",
    "request_id_option": "--request-id",
    "capabilities_option": "--component-capabilities",
}
COMPONENT = "pg_configurator"

EXIT_CODES = {
    "success": 0,
    "validation_error": 2,
    "precondition_failed": 3,
    "unsupported": 4,
    "partial": 5,
    "execution_error": 6,
    "cancelled": 7,
    "ownership_error": 8,
}


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def canonical_hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def artifact_hash(artifact: dict[str, Any]) -> str:
    stable = {
        key: value
        for key, value in artifact.items()
        if key not in {"generated_at", "artifact_hash"}
    }
    return canonical_hash(stable)


def capabilities() -> dict[str, Any]:
    return {
        "capability_schema_version": CAPABILITY_SCHEMA_VERSION,
        "machine_interface": MACHINE_INTERFACE,
        "contract_version": CONTRACT_VERSION,
        "component": COMPONENT,
        "component_version": __version__,
        "commands": {
            "generate": {
                "mutates_target": False,
                "machine_output": True,
                "accepts_input_json": True,
                "accepts_plan_hash": False,
            },
            "validate-input": {
                "mutates_target": False,
                "machine_output": True,
                "accepts_plan_hash": False,
            },
            "capabilities": {
                "mutates_target": False,
                "machine_output": True,
                "accepts_plan_hash": False,
            },
        },
        "artifact_schemas": [
            "pg_configurator/v1",
            "pg_configurator/setting-history-v1",
        ],
        "output_formats": ["conf", "json", "patroni-json"],
        "postgresql_majors": ["9.6", *[str(version) for version in range(10, 19)]],
        "exit_codes": EXIT_CODES,
        "secret_policy": {
            "accepts_secrets": False,
            "stdout_contains_secrets": False,
        },
    }


def envelope(
    command: str,
    status: str,
    *,
    request_id: str | None,
    result: Any = None,
    artifacts: list[dict[str, Any]] | None = None,
    warnings: list[str] | None = None,
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "contract_version": CONTRACT_VERSION,
        "component": COMPONENT,
        "component_version": __version__,
        "command": command,
        "request_id": request_id,
        "status": status,
        "result": result,
        "artifacts": artifacts or [],
        "warnings": warnings or [],
        "error": error,
    }
