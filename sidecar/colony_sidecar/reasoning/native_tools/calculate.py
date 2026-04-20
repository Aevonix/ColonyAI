"""Safe math evaluation tool — no eval(), no exec()."""

from __future__ import annotations

import ast
import logging
import operator
from typing import Any, Dict

logger = logging.getLogger(__name__)

ALLOWED_NAMES = {
    "abs": abs, "round": round, "min": min, "max": max,
    "sum": sum, "len": len, "int": int, "float": float,
    "pow": pow, "divmod": divmod, "sorted": sorted,
    "True": True, "False": False, "None": None,
}

BIN_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub,
    ast.Mult: operator.mul, ast.Div: operator.truediv,
    ast.Mod: operator.mod, ast.Pow: operator.pow,
    ast.FloorDiv: operator.floordiv,
}

COMPARE_OPS = {
    ast.Eq: operator.eq, ast.NotEq: operator.ne,
    ast.Lt: operator.lt, ast.LtE: operator.le,
    ast.Gt: operator.gt, ast.GtE: operator.ge,
}

UNARY_OPS = {
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
    ast.Not: operator.not_,
}


class CalculateTool:
    """Safe math expression evaluator using AST parsing.

    No eval(), no exec(). Only numeric operations allowed.
    """

    @property
    def name(self) -> str:
        return "calculate"

    @property
    def schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "Math expression to evaluate (e.g., '2 + 2', 'abs(-5) * 3')",
                },
            },
            "required": ["expression"],
        }

    async def execute(self, args: Dict[str, Any]) -> Dict[str, Any]:
        expression = args.get("expression", "")
        if not expression:
            return {"error": True, "message": "No expression provided"}

        try:
            tree = ast.parse(expression, mode="eval")
            result = self._eval_node(tree.body)
            return {"result": result, "expression": expression}
        except (ValueError, TypeError, SyntaxError, ZeroDivisionError) as e:
            return {"error": True, "message": str(e), "expression": expression}
        except Exception as e:
            return {"error": True, "message": f"Cannot evaluate: {e}", "expression": expression}

    def _eval_node(self, node: ast.AST) -> Any:
        # Constants (numbers, strings)
        if isinstance(node, ast.Constant):
            return node.value

        # Names (functions, constants)
        if isinstance(node, ast.Name):
            if node.id in ALLOWED_NAMES:
                return ALLOWED_NAMES[node.id]
            raise ValueError(f"Name not allowed: {node.id}")

        # Binary operations
        if isinstance(node, ast.BinOp):
            left = self._eval_node(node.left)
            right = self._eval_node(node.right)
            op_type = type(node.op)
            if op_type in BIN_OPS:
                return BIN_OPS[op_type](left, right)
            raise ValueError(f"Binary operation not allowed: {op_type.__name__}")

        # Unary operations
        if isinstance(node, ast.UnaryOp):
            operand = self._eval_node(node.operand)
            op_type = type(node.op)
            if op_type in UNARY_OPS:
                return UNARY_OPS[op_type](operand)
            raise ValueError(f"Unary operation not allowed: {op_type.__name__}")

        # Comparisons
        if isinstance(node, ast.Compare):
            left = self._eval_node(node.left)
            for op, comparator in zip(node.ops, node.comparators):
                right = self._eval_node(comparator)
                op_type = type(op)
                if op_type in COMPARE_OPS:
                    if not COMPARE_OPS[op_type](left, right):
                        return False
                    left = right
                else:
                    raise ValueError(f"Comparison not allowed: {op_type.__name__}")
            return True

        # Function calls (abs, round, min, max, etc.)
        if isinstance(node, ast.Call):
            func = self._eval_node(node.func)
            call_args = [self._eval_node(a) for a in node.args]
            # Reject keyword arguments for safety
            if node.keywords:
                raise ValueError("Keyword arguments not allowed")
            return func(*call_args)

        # Subscripts (list[0])
        if isinstance(node, ast.Subscript):
            value = self._eval_node(node.value)
            slice_val = self._eval_node(node.slice) if isinstance(node.slice, ast.AST) else node.slice
            return value[slice_val]

        # Lists
        if isinstance(node, ast.List):
            return [self._eval_node(e) for e in node.elts]

        # Tuples
        if isinstance(node, ast.Tuple):
            return tuple(self._eval_node(e) for e in node.elts)

        raise ValueError(f"Operation not allowed: {type(node).__name__}")
