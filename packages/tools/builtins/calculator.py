"""Bounded arithmetic without eval, imports, filesystem or process access."""

from __future__ import annotations

import ast
import math
from collections.abc import Mapping
from typing import Any

from packages.tools.contracts import ToolDefinition, ToolExecutionContext, ToolSessionFactory
from packages.tools.errors import ToolHandlerError

_MAX_EXPRESSION_LENGTH = 256
_MAX_ABS_NUMBER = 10**12
_MAX_ABS_RESULT = 10**18
_MAX_POWER = 12


def _number(node: ast.Constant) -> int | float:
    value = node.value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolHandlerError("CALCULATOR_INVALID_ARGUMENT", "Only finite numbers are allowed.")
    if not math.isfinite(value) or abs(value) > _MAX_ABS_NUMBER:
        raise ToolHandlerError(
            "CALCULATOR_INVALID_ARGUMENT", "The number is outside the safe bound."
        )
    return value


def _evaluate(node: ast.AST) -> int | float:
    if isinstance(node, ast.Constant):
        return _number(node)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _evaluate(node.operand)
        return +value if isinstance(node.op, ast.UAdd) else -value
    if isinstance(node, ast.BinOp):
        left = _evaluate(node.left)
        right = _evaluate(node.right)
        if isinstance(node.op, ast.Add):
            result = left + right
        elif isinstance(node.op, ast.Sub):
            result = left - right
        elif isinstance(node.op, ast.Mult):
            result = left * right
        elif isinstance(node.op, ast.Div):
            if right == 0:
                raise ToolHandlerError(
                    "CALCULATOR_INVALID_ARGUMENT", "Division by zero is not allowed."
                )
            result = left / right
        elif isinstance(node.op, ast.FloorDiv):
            if right == 0:
                raise ToolHandlerError(
                    "CALCULATOR_INVALID_ARGUMENT", "Division by zero is not allowed."
                )
            result = left // right
        elif isinstance(node.op, ast.Mod):
            if right == 0:
                raise ToolHandlerError(
                    "CALCULATOR_INVALID_ARGUMENT", "Division by zero is not allowed."
                )
            result = left % right
        elif isinstance(node.op, ast.Pow):
            if abs(right) > _MAX_POWER or (left == 0 and right < 0):
                raise ToolHandlerError(
                    "CALCULATOR_INVALID_ARGUMENT", "The power is outside the safe bound."
                )
            result = left**right
        else:
            raise ToolHandlerError(
                "CALCULATOR_INVALID_ARGUMENT", "The expression uses an unsupported operator."
            )
        if not isinstance(result, (int, float)) or not math.isfinite(result):
            raise ToolHandlerError("CALCULATOR_INVALID_ARGUMENT", "The result is not finite.")
        if abs(result) > _MAX_ABS_RESULT:
            raise ToolHandlerError(
                "CALCULATOR_INVALID_ARGUMENT", "The result is outside the safe bound."
            )
        return result
    raise ToolHandlerError("CALCULATOR_INVALID_ARGUMENT", "Only bounded arithmetic is allowed.")


async def calculate(
    context: ToolExecutionContext,
    definition: ToolDefinition,
    arguments: Mapping[str, Any],
    session_factory: ToolSessionFactory | None,
) -> dict[str, int | float | str]:
    del context, definition, session_factory
    expression = arguments.get("expression")
    if not isinstance(expression, str) or not expression.strip():
        raise ToolHandlerError(
            "CALCULATOR_INVALID_ARGUMENT", "An arithmetic expression is required."
        )
    if len(expression) > _MAX_EXPRESSION_LENGTH:
        raise ToolHandlerError("CALCULATOR_INVALID_ARGUMENT", "The expression is too long.")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError:
        raise ToolHandlerError(
            "CALCULATOR_INVALID_ARGUMENT", "The expression is invalid."
        ) from None
    return {"expression": expression, "value": _evaluate(tree.body)}


__all__ = ["calculate"]
