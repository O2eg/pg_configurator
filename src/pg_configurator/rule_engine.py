"""Safe evaluator for declarative pg_configurator rule expressions."""

from __future__ import annotations

import ast
import operator
from collections.abc import Callable, Mapping
from typing import Any


class RuleEvaluationError(ValueError):
    """Raised when a rule uses unsupported syntax or cannot be evaluated."""


class RuleEvaluator:
    """Evaluate a deliberately small, side-effect-free Python expression subset."""

    _binary_operators = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
    }
    _comparison_operators = {
        ast.Eq: operator.eq,
        ast.NotEq: operator.ne,
        ast.Lt: operator.lt,
        ast.LtE: operator.le,
        ast.Gt: operator.gt,
        ast.GtE: operator.ge,
        ast.In: lambda left, right: left in right,
        ast.NotIn: lambda left, right: left not in right,
    }

    def __init__(
        self,
        context: Mapping[str, Any],
        *,
        allowed_callables: set[Callable[..., Any]],
        allowed_attribute_roots: set[Any],
    ) -> None:
        self._context = context
        self._allowed_callables = allowed_callables
        self._allowed_attribute_roots = allowed_attribute_roots

    def evaluate(self, expression: str) -> Any:
        try:
            tree = ast.parse(f"({expression.strip()})", mode="eval")
        except SyntaxError as error:
            raise RuleEvaluationError("Invalid rule expression syntax") from error
        return self._evaluate_node(tree.body)

    def _evaluate_node(self, node: ast.AST) -> Any:
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            if node.id not in self._context:
                raise RuleEvaluationError(f"Unknown rule name: {node.id}")
            return self._context[node.id]
        if isinstance(node, ast.List):
            return [self._evaluate_node(item) for item in node.elts]
        if isinstance(node, ast.Tuple):
            return tuple(self._evaluate_node(item) for item in node.elts)
        if isinstance(node, ast.BinOp):
            operation = self._binary_operators.get(type(node.op))
            if operation is None:
                raise RuleEvaluationError("Unsupported binary operator")
            return operation(self._evaluate_node(node.left), self._evaluate_node(node.right))
        if isinstance(node, ast.BoolOp):
            return self._evaluate_boolean_operation(node)
        if isinstance(node, ast.Compare):
            return self._evaluate_comparison(node)
        if isinstance(node, ast.IfExp):
            branch = node.body if self._evaluate_node(node.test) else node.orelse
            return self._evaluate_node(branch)
        if isinstance(node, ast.Attribute):
            root = self._evaluate_node(node.value)
            if root not in self._allowed_attribute_roots or node.attr.startswith("_"):
                raise RuleEvaluationError("Attribute access is not allowed")
            try:
                return getattr(root, node.attr)
            except AttributeError as error:
                raise RuleEvaluationError(f"Unknown rule attribute: {node.attr}") from error
        if isinstance(node, ast.Call):
            function = self._evaluate_node(node.func)
            if function not in self._allowed_callables:
                raise RuleEvaluationError("Function call is not allowed")
            arguments = [self._evaluate_node(argument) for argument in node.args]
            keywords = {
                keyword.arg: self._evaluate_node(keyword.value)
                for keyword in node.keywords
                if keyword.arg is not None
            }
            if len(keywords) != len(node.keywords):
                raise RuleEvaluationError("Expanded keyword arguments are not allowed")
            return function(*arguments, **keywords)
        if isinstance(node, ast.Subscript):
            value = self._evaluate_node(node.value)
            index = self._evaluate_node(node.slice)
            if not isinstance(index, int):
                raise RuleEvaluationError("Only integer indexes are allowed")
            return value[index]
        raise RuleEvaluationError(f"Unsupported rule syntax: {type(node).__name__}")

    def _evaluate_boolean_operation(self, node: ast.BoolOp) -> Any:
        if isinstance(node.op, ast.And):
            result: Any = True
            for value in node.values:
                result = self._evaluate_node(value)
                if not result:
                    return result
            return result
        if isinstance(node.op, ast.Or):
            result = False
            for value in node.values:
                result = self._evaluate_node(value)
                if result:
                    return result
            return result
        raise RuleEvaluationError("Unsupported boolean operator")

    def _evaluate_comparison(self, node: ast.Compare) -> bool:
        left = self._evaluate_node(node.left)
        for operator_node, comparator in zip(node.ops, node.comparators, strict=True):
            operation = self._comparison_operators.get(type(operator_node))
            if operation is None:
                raise RuleEvaluationError("Unsupported comparison operator")
            right = self._evaluate_node(comparator)
            if not operation(left, right):
                return False
            left = right
        return True
