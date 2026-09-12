"""Small static policy for Toolsmith's pure-function capability lane."""

from __future__ import annotations

import ast
from dataclasses import dataclass


SAFE_STDLIB_MODULES = frozenset({
    "base64", "binascii", "bisect", "calendar", "collections", "csv",
    "datetime", "decimal", "difflib", "enum", "fractions", "functools",
    "hashlib", "heapq", "html", "io", "itertools", "json", "math",
    "operator", "re", "statistics", "string", "textwrap", "unicodedata",
    "uuid",
})
DENIED_CALLS = frozenset({
    "__import__", "breakpoint", "compile", "delattr", "eval", "exec",
    "getattr", "globals", "hash", "id", "input", "locals", "open",
    "setattr", "vars",
})
NONDETERMINISTIC_ATTRIBUTES = frozenset({
    "now", "random", "randint", "randrange", "today", "uniform",
    "urandom", "utcnow", "uuid1", "uuid4",
})
DENIED_NODES = (
    ast.AsyncFunctionDef,
    ast.Await,
    ast.ClassDef,
    ast.Delete,
    ast.Global,
    ast.Nonlocal,
    ast.Yield,
    ast.YieldFrom,
)


@dataclass(frozen=True)
class StaticPolicyVerdict:
    allowed: bool
    reasons: tuple[str, ...]


class _PureToolVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.reasons: list[str] = []

    def _deny(self, reason: str) -> None:
        if reason not in self.reasons:
            self.reasons.append(reason)

    def generic_visit(self, node: ast.AST) -> None:
        if isinstance(node, DENIED_NODES):
            self._deny(f"unsupported syntax: {type(node).__name__}")
        super().generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            root = alias.name.split(".", 1)[0]
            if root not in SAFE_STDLIB_MODULES:
                self._deny(f"module is not in the pure allowlist: {root}")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        root = (node.module or "").split(".", 1)[0]
        if node.level or root not in SAFE_STDLIB_MODULES:
            self._deny(f"module is not in the pure allowlist: {root or 'relative'}")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name) and node.func.id in DENIED_CALLS:
            self._deny(f"call is not allowed: {node.func.id}")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr.startswith("__"):
            self._deny("dunder attribute access is not allowed")
        if node.attr in NONDETERMINISTIC_ATTRIBUTES:
            self._deny(f"nondeterministic attribute is not allowed: {node.attr}")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id.startswith("__"):
            self._deny("dunder name access is not allowed")


def validate_pure_tool(source_code: str, test_source: str) -> StaticPolicyVerdict:
    """Validate source and its self-test before either reaches the sandbox."""

    reasons: list[str] = []
    trees: list[tuple[str, ast.Module]] = []
    for label, source in (("source", source_code), ("test", test_source)):
        try:
            trees.append((label, ast.parse(source or "", filename=f"<{label}>")))
        except SyntaxError:
            reasons.append(f"{label} is not valid Python")
    if reasons:
        return StaticPolicyVerdict(False, tuple(reasons))

    source_tree = trees[0][1]
    run_defs = [
        node for node in source_tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "run"
    ]
    if len(run_defs) != 1:
        reasons.append("source must define exactly one top-level run function")
    visitor = _PureToolVisitor()
    for _, tree in trees:
        visitor.visit(tree)
    reasons.extend(visitor.reasons)
    return StaticPolicyVerdict(not reasons, tuple(dict.fromkeys(reasons)))
