"""Tests for the generated data layer consumed by the JavaScript port.

These tests are the freshness and completeness gate described in the web plan.
They exist to make one class of failure impossible: a Python rule, snapshot
column, CLI option or evaluator capability changing without the exported data
following it. A stale export must fail here, in Python CI, rather than in a
browser.
"""

import importlib.util
import json
import unittest
from pathlib import Path

from pg_configurator import configurator as configurator_module
from pg_configurator.conf_common import (
    EXTENSION_PRELOAD_ORDER,
    EXTENSION_SPECS,
    MANDATORY_COMMON_EXTENSIONS,
    PROFILE_EXTENSION_DEPENDENCIES,
    common_alg_set,
    common_profile_alg_sets,
)
from pg_configurator.conf_perf import perf_alg_set
from pg_configurator.configurator import PGConfigurator
from pg_configurator.rule_engine import RuleEvaluator

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "web" / "data"


def _load_exporter():
    spec = importlib.util.spec_from_file_location(
        "export_web_data", ROOT / "tools" / "export_web_data.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


export_web_data = _load_exporter()


def _payload(name):
    return json.loads((DATA_DIR / name).read_text(encoding="utf-8"))["payload"]


def _callable_name(function):
    """Name a permitted callable the way the static export names it."""

    qualname = getattr(function, "__qualname__", getattr(function, "__name__", None))
    if qualname is None:
        raise AssertionError(f"Permitted callable has no name: {function!r}")
    if "<locals>." in qualname:
        return qualname.rsplit("<locals>.", 1)[1]
    return qualname


class TestExportFreshness(unittest.TestCase):
    def test_committed_data_matches_a_fresh_export(self):
        self.assertEqual(
            0,
            export_web_data.main(["--check"]),
            "web/data is stale; run: python tools/export_web_data.py",
        )

    def test_export_is_byte_deterministic(self):
        first = {
            name: export_web_data.render(doc) for name, doc in export_web_data.documents().items()
        }
        second = {
            name: export_web_data.render(doc) for name, doc in export_web_data.documents().items()
        }
        self.assertEqual(first, second)

    def test_export_is_unaffected_by_a_prior_calculation(self):
        # make_conf tags rules with _source and, through prepare_alg_set,
        # that tag reaches the module-level rule data. The export describes the
        # rules as authored, so calculating a configuration first must not
        # change a single byte of it.
        from types import SimpleNamespace

        before = {
            name: export_web_data.render(doc) for name, doc in export_web_data.documents().items()
        }
        PGConfigurator(SimpleNamespace(output_file_name="", debug=False), None).make_conf(
            "8", "32Gi", pg_version="18", conf_profiles="profile_1c"
        )
        after = {
            name: export_web_data.render(doc) for name, doc in export_web_data.documents().items()
        }
        self.assertEqual(before, after)

    def test_documents_carry_stable_metadata_only(self):
        for name in ("rules.json", "pg_settings.json", "input-schema.json"):
            document = json.loads((DATA_DIR / name).read_text(encoding="utf-8"))
            self.assertEqual(export_web_data.SCHEMA_VERSION, document["schema_version"])
            self.assertTrue(document["payload_digest"].startswith("sha256:"))
            # A timestamp or commit would make --check fail on an unchanged
            # checkout; provenance that varies belongs on the built page.
            self.assertNotIn("generated_at", document)
            self.assertNotIn("source_commit", document)


class TestRuleCorpusCompleteness(unittest.TestCase):
    def setUp(self):
        self.payload = _payload("rules.json")

    def _live_sources(self):
        sources = {
            "perf": perf_alg_set,
            "common": common_alg_set,
        }
        for profile, spec in PGConfigurator.conf_profiles.items():
            sources[f"profiles:{profile}"] = spec["alg_set"]
        for profile, versioned in common_profile_alg_sets.items():
            sources[f"common_profiles:{profile}"] = versioned
        return {
            label: export_web_data.sanitize_rule_set(versioned)
            for label, versioned in sources.items()
        }

    def _live_rules(self):
        for label, versioned in self._live_sources().items():
            for version, rules in versioned.items():
                for rule in rules:
                    yield label, version, rule

    def test_every_authoritative_rule_source_is_exported(self):
        exported = self.payload["rule_sets"]
        sanitize = export_web_data.sanitize_rule_set
        self.assertEqual(sanitize(perf_alg_set), exported["perf"])
        self.assertEqual(sanitize(common_alg_set), exported["common"])
        self.assertEqual(
            {
                name: sanitize(spec["alg_set"])
                for name, spec in PGConfigurator.conf_profiles.items()
            },
            exported["profiles"],
        )
        self.assertEqual(
            {name: sanitize(versioned) for name, versioned in common_profile_alg_sets.items()},
            exported["common_profiles"],
        )
        self.assertEqual(
            {"perf", "common", "profiles", "common_profiles"},
            set(exported),
            "an unexported rule source appeared",
        )

    def test_rule_entry_count_matches_the_live_corpus(self):
        self.assertEqual(
            sum(1 for _ in self._live_rules()),
            self.payload["counts"]["rule_entries"],
        )

    def test_every_executable_expression_is_exported(self):
        sentinel = self.payload["deprecated_sentinel"]
        live = {
            rule["alg"]
            for _, _, rule in self._live_rules()
            if "alg" in rule and rule["alg"] != sentinel
        }
        self.assertEqual(live, set(self.payload["expressions"]))

    def test_the_deprecated_sentinel_is_never_an_expression(self):
        sentinel = self.payload["deprecated_sentinel"]
        self.assertNotIn(sentinel, self.payload["expressions"])
        live_sentinels = sum(1 for _, _, rule in self._live_rules() if rule.get("alg") == sentinel)
        self.assertEqual(live_sentinels, self.payload["counts"]["deprecated_sentinels"])
        self.assertGreater(live_sentinels, 0, "the sentinel test would be vacuous")

    def test_stored_asts_match_a_fresh_parse(self):
        for expression, tree in self.payload["expressions"].items():
            with self.subTest(expression=expression):
                self.assertEqual(export_web_data.encode_expression(expression), tree)

    def test_rule_key_and_unit_domains_match_the_corpus(self):
        keys, to_units, postfixes = set(), set(), set()
        for _, _, rule in self._live_rules():
            keys.update(rule)
            if "to_unit" in rule:
                to_units.add(rule["to_unit"])
            if "unit_postfix" in rule:
                postfixes.add(rule["unit_postfix"])
        self.assertTrue(keys.issubset(set(self.payload["rule_key_domain"])))
        self.assertEqual(sorted(to_units), self.payload["to_unit_domain"])
        self.assertEqual(sorted(postfixes), self.payload["unit_postfix_domain"])

    def test_extension_metadata_matches(self):
        extensions = self.payload["extensions"]
        self.assertEqual(sorted(EXTENSION_SPECS), sorted(extensions["specs"]))
        for name, spec in EXTENSION_SPECS.items():
            self.assertEqual(spec["provider"], extensions["specs"][name]["provider"])
            self.assertEqual(
                list(spec["supported_versions"]),
                extensions["specs"][name]["supported_versions"],
            )
            self.assertEqual(
                spec["settings_validation"],
                extensions["specs"][name]["settings_validation"],
            )
        self.assertEqual(sorted(MANDATORY_COMMON_EXTENSIONS), extensions["mandatory_common"])
        self.assertEqual(list(EXTENSION_PRELOAD_ORDER), extensions["preload_order"])
        self.assertEqual(
            {profile: sorted(names) for profile, names in PROFILE_EXTENSION_DEPENDENCIES.items()},
            extensions["profile_dependencies"],
        )


class TestEvaluationContract(unittest.TestCase):
    """Hold the statically extracted contract against what actually runs."""

    def setUp(self):
        self.payload = _payload("rules.json")
        self.recorded = {}

        original = configurator_module.RuleEvaluator

        def recording(context, *, allowed_callables, allowed_attribute_roots):
            self.recorded = {
                "context": dict(context),
                "callables": allowed_callables,
                "attribute_roots": allowed_attribute_roots,
            }
            return original(
                context,
                allowed_callables=allowed_callables,
                allowed_attribute_roots=allowed_attribute_roots,
            )

        configurator_module.RuleEvaluator = recording
        self.addCleanup(setattr, configurator_module, "RuleEvaluator", original)

        from types import SimpleNamespace

        configurator = PGConfigurator(SimpleNamespace(output_file_name="", debug=False), None)
        configurator.make_conf("8", "32Gi", pg_version="18")

    def test_context_bindings_match_a_real_run(self):
        self.assertEqual(
            sorted(self.recorded["context"]),
            self.payload["evaluation_contract"]["context_bindings"],
        )

    def test_permitted_callables_match_a_real_run(self):
        self.assertEqual(
            sorted(_callable_name(item) for item in self.recorded["callables"]),
            self.payload["evaluation_contract"]["callables"],
        )

    def test_permitted_attribute_roots_match_a_real_run(self):
        self.assertEqual(
            sorted(root.__name__ for root in self.recorded["attribute_roots"]),
            self.payload["evaluation_contract"]["attribute_roots"],
        )

    def test_operator_tables_are_fully_encodable(self):
        # The exporter refuses to run when RuleEvaluator gains an operator the
        # JavaScript walker does not implement; assert the gate is wired.
        self.assertEqual(
            set(RuleEvaluator._binary_operators),
            set(export_web_data.BINARY_OPERATORS),
        )
        self.assertEqual(
            set(RuleEvaluator._comparison_operators),
            set(export_web_data.COMPARISON_OPERATORS),
        )


class TestSnapshotPacking(unittest.TestCase):
    def setUp(self):
        self.payload = _payload("pg_settings.json")

    def test_packing_is_lossless_for_every_version(self):
        for version in PGConfigurator.known_versions:
            with self.subTest(version=version):
                self.assertEqual(
                    export_web_data._read_snapshot(version),
                    export_web_data.reconstruct_snapshot(self.payload, version),
                )

    def test_exported_columns_cover_validation_and_explanation(self):
        # _validate_setting_value reads vartype/enumvals/min_val/max_val/unit;
        # _setting_context reads context. Dropping one silently weakens
        # validation in the browser only.
        self.assertEqual(
            ["vartype", "unit", "context", "min_val", "max_val", "enumvals"],
            self.payload["columns"],
        )

    def test_row_counts_match_the_snapshots(self):
        for version in PGConfigurator.known_versions:
            self.assertEqual(
                len(export_web_data._read_snapshot(version)),
                self.payload["counts"]["rows_by_version"][version],
            )


class TestInputSchema(unittest.TestCase):
    def setUp(self):
        self.payload = _payload("input-schema.json")
        self.by_dest = {option["dest"]: option for option in self.payload["options"]}

    def test_every_parser_option_is_exported(self):
        import argparse

        parser = PGConfigurator.get_arg_parser()
        live = {
            action.dest
            for action in parser._actions
            if not isinstance(action, argparse._HelpAction)
        }
        self.assertEqual(live, set(self.by_dest))

    def test_every_make_conf_parameter_is_reachable(self):
        import inspect

        expected = {
            name
            for name in inspect.signature(PGConfigurator.make_conf).parameters
            if name != "self"
        }
        mapped = {
            option["make_conf_parameter"]
            for option in self.payload["options"]
            if option["make_conf_parameter"]
        }
        self.assertEqual(expected, mapped)
        self.assertEqual(len(expected), self.payload["counts"]["calculation"])

    def test_host_derived_defaults_are_not_baked_into_the_export(self):
        for dest in ("db_cpu", "db_ram"):
            option = self.by_dest[dest]
            self.assertIsNone(option["default"], f"{dest} default must not be a host value")
            self.assertEqual("host", option["default_source"])

    def test_orchestration_options_are_classified(self):
        self.assertEqual(
            configurator_module._ORCHESTRATION_ONLY_DESTINATIONS,
            {
                option["dest"]
                for option in self.payload["options"]
                if option["role"] == "orchestration"
            },
        )

    def test_form_excludes_only_documented_inputs(self):
        excluded = {
            option["make_conf_parameter"]
            for option in self.payload["options"]
            if option["role"] == "calculation" and not option["form_field"]
        }
        self.assertEqual(set(export_web_data.FORM_EXCLUSIONS), excluded)
        for option in self.payload["options"]:
            if option["role"] == "calculation" and not option["form_field"]:
                self.assertTrue(option["form_exclusion_reason"])

    def test_abbreviation_policy_is_recorded(self):
        # argparse accepts unambiguous long-option prefixes; the Node CLI has
        # to reproduce or explicitly refuse that, so the policy is data.
        self.assertIn("allow_abbrev", self.payload["parser"])
        self.assertIsInstance(self.payload["parser"]["allow_abbrev"], bool)


if __name__ == "__main__":
    unittest.main()
