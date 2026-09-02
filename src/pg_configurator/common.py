import inspect
import re
import sys
import traceback
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class BasicEnum:
    def __str__(self):
        return self.value


class ResultCode(BasicEnum, Enum):
    DONE = "done"
    FAIL = "fail"
    UNKNOWN = "unknown"


@dataclass
class PGConfiguratorResult:
    result_code: ResultCode = ResultCode.UNKNOWN
    result_data: Any = None
    artifact: dict[str, Any] | None = None
    advisories: list[dict[str, Any]] = field(default_factory=list)


def exception_helper(show_traceback=True):
    exc_type, exc_value, exc_traceback = sys.exc_info()
    return "\n".join(
        [
            v
            for v in traceback.format_exception(
                exc_type, exc_value, exc_traceback if show_traceback else None
            )
        ]
    )


def get_default_args(func):
    signature = inspect.signature(func)
    return {
        k: v.default
        for k, v in signature.parameters.items()
        if v.default is not inspect.Parameter.empty
    }


def get_major_version(str_version):
    match = re.search(r"\d+", str(str_version))
    if match is None:
        raise ValueError(f"Invalid PostgreSQL version: {str_version}")
    return int(match.group(0))


def print_header(header):
    print("\n\n")
    print("=".join(["=" * 100]))
    print(header)
    print("=".join(["=" * 100]))


def recordset_to_list_flat(rs):
    res = []
    for rec in rs:
        row = []
        for _, v in dict(rec).items():
            row.append(v)
        res.append(row)
    return res
