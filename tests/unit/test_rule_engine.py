import unittest

from pg_configurator.rule_engine import RuleEvaluationError, RuleEvaluator


class TestRuleEvaluator(unittest.TestCase):
    def evaluator(self, context=None, callables=None, roots=None):
        return RuleEvaluator(
            context or {},
            allowed_callables=set(callables or ()),
            allowed_attribute_roots=set(roots or ()),
        )

    def test_evaluates_supported_expression_subset(self):
        def scale(value):
            return value * 2

        evaluator = self.evaluator(
            {"enabled": True, "scale": scale, "value": 4},
            {scale},
        )

        self.assertEqual(10, evaluator.evaluate("scale(value) + 2 if enabled else 0"))

    def test_rejects_unknown_functions_and_object_introspection(self):
        expressions = (
            "__import__('os').system('echo unsafe')",
            "().__class__",
            "[item for item in (1, 2)]",
            "(lambda: 1)()",
        )

        for expression in expressions:
            with self.subTest(expression=expression):
                with self.assertRaises(RuleEvaluationError):
                    self.evaluator().evaluate(expression)


if __name__ == "__main__":
    unittest.main()
