#!/usr/bin/env python3
"""Export the data layer consumed by the JavaScript port under ``web/``.

The JavaScript implementation must never re-type the declarative rules, the
``pg_settings`` snapshots or the input surface: it reads them from generated
JSON produced here, out of the same Python sources the CLI uses. Three files
are written into ``web/data``:

``rules.json``
    Every rule set verbatim, the profile registry, extension metadata, the
    enum roots reachable from rule expressions, and one parsed abstract syntax
    tree per distinct executable expression.

``pg_settings.json``
    The snapshot columns needed for validation and ``apply_mode`` explanation,
    packed as a base version plus per-version differences.

``input-schema.json``
    The public input surface: the argparse layer the Node CLI is built from,
    the mapping onto ``make_conf`` parameters, and the flags the browser form
    needs. A new CLI option cannot reach either front end without appearing
    here first.

Determinism is a requirement, not a nicety: ``--check`` renders a fresh export
in memory and compares it with the committed files, so the output must not
embed a timestamp, a commit or anything else that varies between runs on the
same sources. Provenance that does vary (the source commit) is stamped onto
the built page instead.

Expressions are parsed here, with the same ``ast`` module ``RuleEvaluator``
uses, so the two implementations cannot disagree about what an expression
means. Any node type or operator outside the supported set aborts the export:
a new Python rule using new syntax fails CI rather than the browser.

Usage::

    python tools/export_web_data.py            # write web/data/*.json
    python tools/export_web_data.py --check    # fail if the files are stale
"""

from __future__ import annotations

import argparse
import ast
import csv
import inspect
import json
import pathlib
import sys
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from pg_configurator import configurator as configurator_module  # noqa: E402
from pg_configurator.conf_common import (  # noqa: E402
    EXTENSION_PRELOAD_ORDER,
    EXTENSION_SPECS,
    MANDATORY_COMMON_EXTENSIONS,
    PROFILE_EXTENSION_DEPENDENCIES,
    SUPPORTED_PG_MAJORS,
    common_alg_set,
    common_profile_alg_sets,
)
from pg_configurator.conf_perf import perf_alg_set  # noqa: E402
from pg_configurator.configurator import (  # noqa: E402
    DiskType,
    DutyDB,
    OutputFormat,
    PGConfigurator,
    Platform,
    ReplicationMode,
)
from pg_configurator.orchestration import canonical_hash  # noqa: E402
from pg_configurator.rule_engine import RuleEvaluator  # noqa: E402
from pg_configurator.version import __version__  # noqa: E402

DATA_DIR = ROOT / "web" / "data"
SCHEMA_VERSION = "pg_configurator/web-data-v1"

# ``{"name": ..., "alg": "deprecated"}`` is a sentinel handled by
# prepare_alg_set, not an expression. It parses as a bare Name and would reach
# the browser as an unresolvable rule reference, so it is excluded from the
# expression table by name rather than by syntax.
DEPRECATED_SENTINEL = "deprecated"

# Keys that exist only while a configuration is being calculated. ``make_conf``
# tags rules with ``_source`` and, because ``prepare_alg_set`` returns entries
# that alias the module-level mappings rather than its own deep copy, the tag
# reaches the bundled rule data and stays there for the life of the process.
# The export must describe the rules as authored, so it strips them; a rule key
# that is not runtime-only and not in the known domain still aborts the export.
RUNTIME_ONLY_RULE_KEYS = frozenset({"_source"})

# Defaults that argparse reads from the host through psutil. Baking them into
# a generated file would make the export machine-dependent and break --check on
# any other machine, so they are exported as null with the source recorded.
HOST_DERIVED_DESTINATIONS = frozenset({"db_cpu", "db_ram"})

# Calculation inputs the browser form deliberately does not expose. Both remain
# available through the CLI and through an imported input document.
FORM_EXCLUSIONS = {
    "replication_enabled": "compatibility alias; the form exposes replication_mode",
    "common_conf": "disabling it is rejected by make_conf",
    "available_extensions": (
        "a caller assertion about the target that changes no generated setting: "
        "it swaps one warning for another and flips artifact metadata"
    ),
}

# The expression syntax the JavaScript walker implements. Kept as an explicit
# whitelist rather than derived from RuleEvaluator, then cross-checked against
# it: when Python gains an operator the export fails loudly instead of emitting
# something the port cannot evaluate.
BINARY_OPERATORS = {ast.Add: "Add", ast.Sub: "Sub", ast.Mult: "Mult", ast.Div: "Div"}
COMPARISON_OPERATORS = {
    ast.Eq: "Eq",
    ast.NotEq: "NotEq",
    ast.Lt: "Lt",
    ast.LtE: "LtE",
    ast.Gt: "Gt",
    ast.GtE: "GtE",
    ast.In: "In",
    ast.NotIn: "NotIn",
}
BOOLEAN_OPERATORS = {ast.And: "And", ast.Or: "Or"}

ENUM_ROOTS = {
    "DiskType": DiskType,
    "DutyDB": DutyDB,
    "OutputFormat": OutputFormat,
    "Platform": Platform,
    "ReplicationMode": ReplicationMode,
}

SNAPSHOT_COLUMNS = ("vartype", "unit", "context", "min_val", "max_val", "enumvals")
SNAPSHOT_BASE_VERSION = "18"


class ExportError(RuntimeError):
    """Raised when a source cannot be represented for the JavaScript port."""


# --------------------------------------------------------------------------
# expressions
# --------------------------------------------------------------------------


def _assert_operator_tables_match() -> None:
    """Fail if RuleEvaluator supports syntax the JavaScript walker does not."""

    for label, exported, supported in (
        ("binary", BINARY_OPERATORS, RuleEvaluator._binary_operators),
        ("comparison", COMPARISON_OPERATORS, RuleEvaluator._comparison_operators),
    ):
        missing = set(supported) - set(exported)
        if missing:
            names = ", ".join(sorted(node.__name__ for node in missing))
            raise ExportError(
                f"RuleEvaluator supports {label} operators the export cannot encode: {names}. "
                "Implement them in web/src/rule-eval.js and add them here."
            )


def _constant_kind(value: Any) -> str:
    if value is None:
        return "none"
    if isinstance(value, bool):  # bool before int: bool is a subclass of int
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    raise ExportError(f"Unsupported constant type in a rule expression: {type(value).__name__}")


def encode_expression_node(node: ast.AST) -> dict[str, Any]:
    """Encode one node of the supported Python expression subset as JSON.

    The integer/float distinction is preserved explicitly. JavaScript has one
    numeric type, and losing the distinction would change ``int()`` results and
    the formatted values printed for ``to_unit: as_is`` rules.
    """

    if isinstance(node, ast.Constant):
        return {"t": "Const", "k": _constant_kind(node.value), "v": node.value}
    if isinstance(node, ast.Name):
        return {"t": "Name", "id": node.id}
    if isinstance(node, (ast.List, ast.Tuple)):
        kind = "List" if isinstance(node, ast.List) else "Tuple"
        return {"t": kind, "elements": [encode_expression_node(item) for item in node.elts]}
    if isinstance(node, ast.BinOp):
        operator = BINARY_OPERATORS.get(type(node.op))
        if operator is None:
            raise ExportError(f"Unsupported binary operator: {type(node.op).__name__}")
        return {
            "t": "BinOp",
            "op": operator,
            "left": encode_expression_node(node.left),
            "right": encode_expression_node(node.right),
        }
    if isinstance(node, ast.BoolOp):
        operator = BOOLEAN_OPERATORS.get(type(node.op))
        if operator is None:
            raise ExportError(f"Unsupported boolean operator: {type(node.op).__name__}")
        return {
            "t": "BoolOp",
            "op": operator,
            "values": [encode_expression_node(value) for value in node.values],
        }
    if isinstance(node, ast.Compare):
        operators = []
        for operator_node in node.ops:
            operator = COMPARISON_OPERATORS.get(type(operator_node))
            if operator is None:
                raise ExportError(
                    f"Unsupported comparison operator: {type(operator_node).__name__}"
                )
            operators.append(operator)
        return {
            "t": "Compare",
            "left": encode_expression_node(node.left),
            "ops": operators,
            "comparators": [encode_expression_node(item) for item in node.comparators],
        }
    if isinstance(node, ast.IfExp):
        return {
            "t": "IfExp",
            "test": encode_expression_node(node.test),
            "body": encode_expression_node(node.body),
            "orelse": encode_expression_node(node.orelse),
        }
    if isinstance(node, ast.Attribute):
        if node.attr.startswith("_"):
            raise ExportError(f"Private attribute access is not allowed: {node.attr}")
        return {
            "t": "Attribute",
            "value": encode_expression_node(node.value),
            "attr": node.attr,
        }
    if isinstance(node, ast.Call):
        for keyword in node.keywords:
            if keyword.arg is None:
                raise ExportError("Expanded keyword arguments are not allowed in a rule")
        return {
            "t": "Call",
            "func": encode_expression_node(node.func),
            "args": [encode_expression_node(argument) for argument in node.args],
            "keywords": [
                {"name": keyword.arg, "value": encode_expression_node(keyword.value)}
                for keyword in node.keywords
            ],
        }
    if isinstance(node, ast.Subscript):
        return {
            "t": "Subscript",
            "value": encode_expression_node(node.value),
            "index": encode_expression_node(node.slice),
        }
    raise ExportError(f"Unsupported rule syntax: {type(node).__name__}")


def encode_expression(expression: str) -> dict[str, Any]:
    """Parse one rule expression exactly the way RuleEvaluator parses it."""

    tree = ast.parse(f"({expression.strip()})", mode="eval")
    return encode_expression_node(tree.body)


# --------------------------------------------------------------------------
# rule sets
# --------------------------------------------------------------------------


def _evaluation_contract() -> dict[str, Any]:
    """Read the evaluator's permitted surface out of ``make_conf`` itself.

    ``RuleEvaluator`` receives live function objects and enum classes, so the
    permitted surface exists only at run time. Reading the call site statically
    turns it into data the port can be held to: today only 5 of the permitted
    callables and 1 of the attribute roots are referenced by an actual rule, so
    the remainder would be untested code in JavaScript until some future Python
    rule silently activates it.

    The context bindings are the names the evaluator must resolve. Exporting
    them lets the port assert it binds exactly the same set instead of
    discovering a missing name through a wrong configuration value.
    """

    tree = ast.parse(inspect.getsource(configurator_module))
    make_conf = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "make_conf"
        ),
        None,
    )
    if make_conf is None:
        raise ExportError("Could not locate make_conf")

    def _reference_name(node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            return f"{node.value.id}.{node.attr}"
        raise ExportError("Unexpected entry in the evaluator's permitted surface")

    call = next(
        (
            node
            for node in ast.walk(make_conf)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "RuleEvaluator"
        ),
        None,
    )
    if call is None:
        raise ExportError("Could not locate the RuleEvaluator construction in make_conf")

    surface: dict[str, list[str]] = {}
    for keyword in call.keywords:
        if keyword.arg not in ("allowed_callables", "allowed_attribute_roots"):
            continue
        if not isinstance(keyword.value, ast.Set):
            raise ExportError(f"{keyword.arg} is no longer a set literal")
        surface[keyword.arg] = sorted(_reference_name(item) for item in keyword.value.elts)

    context = next(
        (
            node.value
            for node in ast.walk(make_conf)
            if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Dict)
            and any(
                isinstance(target, ast.Name) and target.id == "rule_context"
                for target in node.targets
            )
        ),
        None,
    )
    if context is None:
        raise ExportError("Could not locate the rule_context assembly in make_conf")
    bindings = []
    for key in context.keys:
        if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
            raise ExportError("rule_context is no longer keyed by string literals")
        bindings.append(key.value)

    return {
        "callables": surface.get("allowed_callables", []),
        "attribute_roots": surface.get("allowed_attribute_roots", []),
        "context_bindings": sorted(bindings),
    }


def sanitize_rule_set(
    versioned: dict[str, list[dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    """Copy one versioned rule set, dropping calculation-time markers."""

    return {
        version: [
            {key: value for key, value in rule.items() if key not in RUNTIME_ONLY_RULE_KEYS}
            for rule in rules
        ]
        for version, rules in versioned.items()
    }


def _profile_descriptions() -> dict[str, str | None]:
    """Take each profile's one-line summary from the module that defines it.

    Matched by the identity of the rule set rather than by a name convention,
    so a renamed module cannot silently attach the wrong summary. A profile
    whose module has no docstring gets ``None``: inventing a description here
    would put words in the source's mouth.
    """

    import importlib
    import pkgutil

    from pg_configurator import conf_profiles as package

    by_alg_set: dict[int, str | None] = {}
    for module_info in pkgutil.iter_modules(package.__path__):
        module = importlib.import_module(f"{package.__name__}.{module_info.name}")
        summary = (module.__doc__ or "").strip().split("\n")[0] or None
        for value in vars(module).values():
            if isinstance(value, dict):
                by_alg_set[id(value)] = summary

    return {
        name: by_alg_set.get(id(spec["alg_set"]))
        for name, spec in PGConfigurator.conf_profiles.items()
    }


def _rule_sets() -> dict[str, Any]:
    return {
        "perf": sanitize_rule_set(perf_alg_set),
        "common": sanitize_rule_set(common_alg_set),
        "profiles": {
            name: sanitize_rule_set(spec["alg_set"])
            for name, spec in PGConfigurator.conf_profiles.items()
        },
        "common_profiles": {
            name: sanitize_rule_set(versioned)
            for name, versioned in common_profile_alg_sets.items()
        },
    }


def _iterate_rules(rule_sets: dict[str, Any]):
    """Yield every rule entry in the corpus with a readable location."""

    for set_name, payload in rule_sets.items():
        if set_name in ("profiles", "common_profiles"):
            for profile, versioned in payload.items():
                for version, rules in versioned.items():
                    for rule in rules:
                        yield f"{set_name}:{profile}:{version}", rule
        else:
            for version, rules in payload.items():
                for rule in rules:
                    yield f"{set_name}:{version}", rule


def export_rules() -> dict[str, Any]:
    _assert_operator_tables_match()
    rule_sets = _rule_sets()

    known_keys = {"name", "const", "alg", "to_unit", "unit_postfix", "__parent"}
    expressions: dict[str, Any] = {}
    to_units: set[str] = set()
    unit_postfixes: set[str] = set()
    sentinel_count = 0

    for location, rule in _iterate_rules(rule_sets):
        unknown = set(rule) - known_keys
        if unknown:
            raise ExportError(
                f"Rule at {location} uses keys the port does not implement: "
                + ", ".join(sorted(unknown))
            )
        if "to_unit" in rule:
            to_units.add(rule["to_unit"])
        if "unit_postfix" in rule:
            unit_postfixes.add(rule["unit_postfix"])
        expression = rule.get("alg")
        if expression is None:
            continue
        if expression == DEPRECATED_SENTINEL:
            sentinel_count += 1
            continue
        if expression not in expressions:
            try:
                expressions[expression] = encode_expression(expression)
            except SyntaxError as error:
                raise ExportError(f"Rule at {location} is not parseable: {error}") from error

    return {
        "supported_versions": list(SUPPORTED_PG_MAJORS),
        "known_versions": list(PGConfigurator.known_versions),
        "rule_key_domain": sorted(known_keys),
        "deprecated_sentinel": DEPRECATED_SENTINEL,
        "to_unit_domain": sorted(to_units),
        "unit_postfix_domain": sorted(unit_postfixes),
        "rule_sets": rule_sets,
        "expressions": expressions,
        "evaluation_contract": _evaluation_contract(),
        "profiles": {
            name: {
                "supported_versions": list(spec["supported_versions"]),
                "exclusive": name == "profile_1c",
                "description": _profile_descriptions().get(name),
            }
            for name, spec in PGConfigurator.conf_profiles.items()
        },
        # Ordered pairs, not a mapping: Python reports enum choices in
        # declaration order ("statistic, mixed, oltp, financial"), and a JSON
        # object rendered with sorted keys would silently reorder them into a
        # different error message.
        "enums": {
            name: [[member.name, member.value] for member in enum_type]
            for name, enum_type in ENUM_ROOTS.items()
        },
        "extensions": {
            "specs": {
                name: {
                    "provider": spec["provider"],
                    "supported_versions": list(spec["supported_versions"]),
                    "settings_validation": spec["settings_validation"],
                }
                for name, spec in EXTENSION_SPECS.items()
            },
            "mandatory_common": sorted(MANDATORY_COMMON_EXTENSIONS),
            "profile_dependencies": {
                profile: sorted(names) for profile, names in PROFILE_EXTENSION_DEPENDENCIES.items()
            },
            "preload_order": list(EXTENSION_PRELOAD_ORDER),
        },
        "restart_required_settings": sorted(PGConfigurator.restart_required_settings),
        "counts": {
            "rule_entries": sum(1 for _ in _iterate_rules(rule_sets)),
            "deprecated_sentinels": sentinel_count,
            "distinct_expressions": len(expressions),
        },
    }


# --------------------------------------------------------------------------
# pg_settings snapshots
# --------------------------------------------------------------------------


def _read_snapshot(version: str) -> dict[str, list[str]]:
    path = (
        pathlib.Path(PGConfigurator.current_dir)
        / "pg_settings_history"
        / PGConfigurator.known_versions[version]
    )
    with path.open(encoding="utf-8") as handle:
        rows = {}
        for row in csv.DictReader(handle):
            name = row.get("name")
            if not name:
                continue
            rows[name] = [row.get(column) or "" for column in SNAPSHOT_COLUMNS]
    return rows


def export_pg_settings() -> dict[str, Any]:
    """Pack the snapshots as a base version plus per-version differences.

    Lossless by construction: a setting missing from the base is written whole
    into the version that has it, and a setting the base has but a version does
    not is listed as absent. ``reconstruct_snapshot`` is the inverse and the
    exporter's test asserts the round trip for every version and column.
    """

    versions = list(PGConfigurator.known_versions)
    snapshots = {version: _read_snapshot(version) for version in versions}
    base = snapshots[SNAPSHOT_BASE_VERSION]

    deltas = {}
    for version, rows in snapshots.items():
        if version == SNAPSHOT_BASE_VERSION:
            continue
        deltas[version] = {
            "changed": {name: values for name, values in rows.items() if base.get(name) != values},
            "absent": sorted(set(base) - set(rows)),
        }

    return {
        "columns": list(SNAPSHOT_COLUMNS),
        "base_version": SNAPSHOT_BASE_VERSION,
        "base": base,
        "versions": deltas,
        "counts": {
            "distinct_names": len(set().union(*(set(rows) for rows in snapshots.values()))),
            "rows_by_version": {version: len(rows) for version, rows in snapshots.items()},
        },
    }


def reconstruct_snapshot(payload: dict[str, Any], version: str) -> dict[str, list[str]]:
    """Rebuild one version's snapshot from the packed export."""

    if version == payload["base_version"]:
        return dict(payload["base"])
    delta = payload["versions"][version]
    rows = {
        name: list(values)
        for name, values in payload["base"].items()
        if name not in set(delta["absent"])
    }
    rows.update({name: list(values) for name, values in delta["changed"].items()})
    return rows


# --------------------------------------------------------------------------
# input schema
# --------------------------------------------------------------------------


def _make_conf_argument_mapping() -> dict[str, str]:
    """Read the ``args.* -> make_conf`` mapping out of ``run_pgc`` itself.

    Extracting it from the source rather than restating it means a new
    calculation parameter is picked up automatically, and one that is wired
    into ``make_conf`` without a CLI option is reported as missing.
    """

    tree = ast.parse(inspect.getsource(configurator_module))
    call = None
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "make_conf"
        ):
            call = node
            break
    if call is None:
        raise ExportError("Could not locate the make_conf call in run_pgc")

    signature = inspect.signature(PGConfigurator.make_conf)
    positional = [name for name in signature.parameters if name != "self"]

    mapping: dict[str, str] = {}
    for index, argument in enumerate(call.args):
        if not (isinstance(argument, ast.Attribute) and isinstance(argument.value, ast.Name)):
            raise ExportError("Unexpected positional argument shape in the make_conf call")
        mapping[argument.attr] = positional[index]
    for keyword in call.keywords:
        value = keyword.value
        if not (isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name)):
            raise ExportError(f"Unexpected keyword argument shape for {keyword.arg}")
        mapping[value.attr] = keyword.arg

    unmapped = set(positional) - set(mapping.values())
    if unmapped:
        raise ExportError(
            "make_conf parameters are not reachable from the CLI: " + ", ".join(sorted(unmapped))
        )
    return mapping


def _json_scalar(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if hasattr(value, "value"):  # enum member
        return value.value
    raise ExportError(f"Default value is not representable in JSON: {value!r}")


def _action_kind(action: argparse.Action) -> str:
    if isinstance(action, argparse.BooleanOptionalAction):
        return "boolean_optional"
    if isinstance(action, argparse._StoreTrueAction):
        return "store_true"
    if isinstance(action, argparse._HelpAction):
        return "help"
    if isinstance(action, argparse._StoreAction):
        return "store"
    raise ExportError(f"Unsupported argparse action: {type(action).__name__}")


def _type_name(action: argparse.Action) -> str | None:
    if action.type is None:
        return None
    return getattr(action.type, "__name__", str(action.type))


def _portable_help(action: argparse.Action) -> str | None:
    """Return help text without argparse's version-specific decoration."""
    if action.help == argparse.SUPPRESS:
        return None
    help_text = action.help
    if isinstance(action, argparse.BooleanOptionalAction):
        # Python 3.10 appends this suffix inside BooleanOptionalAction while
        # newer runtimes leave the supplied help unchanged. Generated data must
        # be byte-identical across every Python version in the CI matrix.
        help_text = help_text.removesuffix(" (default: %(default)s)")
    return help_text


def export_input_schema() -> dict[str, Any]:
    parser = PGConfigurator.get_arg_parser()
    mapping = _make_conf_argument_mapping()
    orchestration = set(configurator_module._ORCHESTRATION_ONLY_DESTINATIONS)

    options = []
    for action in parser._actions:
        kind = _action_kind(action)
        if kind == "help":
            continue
        dest = action.dest
        if dest in mapping:
            role = "calculation"
        elif dest in orchestration:
            role = "orchestration"
        else:
            role = "output"

        host_derived = dest in HOST_DERIVED_DESTINATIONS
        choices = None
        if action.choices is not None:
            choices = [_json_scalar(choice) for choice in action.choices]

        entry: dict[str, Any] = {
            "dest": dest,
            "option_strings": list(action.option_strings),
            "primary_option": action.option_strings[0] if action.option_strings else None,
            "action": kind,
            "type": _type_name(action),
            "choices": choices,
            "default": None if host_derived else _json_scalar(action.default),
            "default_source": "host" if host_derived else "static",
            "hidden": action.help == argparse.SUPPRESS,
            "help": _portable_help(action),
            "role": role,
            "make_conf_parameter": mapping.get(dest),
        }
        if role == "calculation":
            reason = FORM_EXCLUSIONS.get(mapping[dest])
            entry["form_field"] = reason is None
            if reason is not None:
                entry["form_exclusion_reason"] = reason
        else:
            entry["form_field"] = False
        options.append(entry)

    # Declaration order, not sorted: argparse reports ambiguous abbreviations in
    # the order the options were added ("--output-format, --output-file-name"),
    # and the form groups fields the same way. The order is deterministic
    # because get_arg_parser builds it the same way every time.
    calculation = [item for item in options if item["role"] == "calculation"]
    return {
        "parser": {
            "allow_abbrev": parser.allow_abbrev,
            "prefix_chars": parser.prefix_chars,
        },
        "options": options,
        "counts": {
            "options": len(options),
            "calculation": len(calculation),
            "form_fields": sum(1 for item in options if item["form_field"]),
            "orchestration": sum(1 for item in options if item["role"] == "orchestration"),
        },
    }


# --------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------


def build_document(payload: dict[str, Any]) -> dict[str, Any]:
    """Wrap a payload with the metadata every generated file carries.

    No timestamp and no commit: the files must be byte-identical for identical
    sources so that ``--check`` means "stale data", not "different machine".
    """

    return {
        "schema_version": SCHEMA_VERSION,
        "package_version": __version__,
        "payload_digest": canonical_hash(payload),
        "generator": "tools/export_web_data.py",
        "payload": payload,
    }


def render(document: dict[str, Any]) -> str:
    return json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def documents() -> dict[str, dict[str, Any]]:
    return {
        "rules.json": build_document(export_rules()),
        "pg_settings.json": build_document(export_pg_settings()),
        "input-schema.json": build_document(export_input_schema()),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="Do not write; fail when the committed files differ from a fresh export",
    )
    parser.add_argument(
        "--out-dir",
        default=str(DATA_DIR),
        help="Directory to write into (default: %(default)s)",
    )
    arguments = parser.parse_args(argv)
    out_dir = pathlib.Path(arguments.out_dir)

    try:
        rendered = {name: render(document) for name, document in documents().items()}
    except ExportError as error:
        print(f"export_web_data: {error}", file=sys.stderr)
        return 2

    if arguments.check:
        stale = []
        for name, content in rendered.items():
            path = out_dir / name
            if not path.exists():
                stale.append(f"{name}: missing")
            elif path.read_text(encoding="utf-8") != content:
                stale.append(f"{name}: out of date")
        if stale:
            print(
                "export_web_data: generated data is stale:\n  "
                + "\n  ".join(stale)
                + "\nRun: python tools/export_web_data.py",
                file=sys.stderr,
            )
            return 1
        print(f"export_web_data: {len(rendered)} files up to date")
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)
    for name, content in rendered.items():
        (out_dir / name).write_text(content, encoding="utf-8")
        print(f"export_web_data: wrote {out_dir / name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
