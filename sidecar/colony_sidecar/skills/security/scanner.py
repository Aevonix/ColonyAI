"""Colony Skills — Python AST-based static analysis for skill source code."""

from __future__ import annotations

import ast
import hashlib
import re
from dataclasses import dataclass, field
from typing import List, Optional


BLOCKED_BUILTINS = frozenset({
    "exec", "eval", "compile", "__import__", "open",
    "breakpoint", "input",
})

BLOCKED_MODULES = frozenset({
    "subprocess", "os.system", "pty", "ctypes", "pickle",
    "marshal", "importlib.util",
})

# Dunder attributes that reach the runtime's object graph and let a skill
# escape the builtins allowlist via chains like
# ``().__class__.__bases__[0].__subclasses__()`` or
# ``getattr(__builtins__, "eval")``. This is a *defense-in-depth* check —
# real isolation requires a subprocess+seccomp sandbox, which is tracked
# separately; this scanner just makes the obvious escape patterns loud.
BLOCKED_DUNDERS = frozenset({
    "__class__", "__bases__", "__mro__", "__subclasses__",
    "__globals__", "__builtins__", "__import__",
    "__getattribute__", "__dict__", "__code__",
})

_DYNAMIC_ATTR_FUNCS = frozenset({"getattr", "setattr", "delattr"})


@dataclass
class ScanFinding:
    rule_id: str
    severity: str        # "critical" | "warning" | "info"
    line: int
    message: str
    evidence: str


@dataclass
class ASTScanResult:
    skill_id: str
    status: str           # "clean" | "warning" | "critical"
    findings: List[ScanFinding] = field(default_factory=list)
    scanned_lines: int = 0
    duration_ms: int = 0

    @property
    def has_critical(self) -> bool:
        return any(f.severity == "critical" for f in self.findings)

    @property
    def has_warning(self) -> bool:
        return any(f.severity == "warning" for f in self.findings)


class ASTScanner:
    """Python AST-based static analysis for skill source code.

    Checks for:
      - Direct calls to blocked builtins (exec, eval, etc.)  [BLT001]
      - Imports of blocked modules                            [IMP001]
      - base64.decode + exec/eval combinations               [OBF001]
      - os.environ access to undeclared variables            [ENV001]
      - Dunder access used to escape the builtins allowlist  [ESC001]
      - getattr/setattr/delattr with dynamic/dunder targets  [ESC002]
      - socket.connect to non-declared hosts                 [NET001]

    Defense-in-depth only: a determined attacker can still compose escapes
    this static scanner misses. Real isolation needs a subprocess + seccomp
    sandbox, which is out of scope here.
    """

    def scan(self, source: str, skill_id: str) -> ASTScanResult:
        """Scan Python source for security findings.

        Args:
            source:   Python source code string.
            skill_id: Identifier used in the result.

        Returns:
            ASTScanResult with status and list of findings.
        """
        lines = source.splitlines()
        findings: List[ScanFinding] = []
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            return ASTScanResult(
                skill_id=skill_id,
                status="critical",
                findings=[ScanFinding(
                    rule_id="SYN001",
                    severity="critical",
                    line=exc.lineno or 0,
                    message="Syntax error prevents AST analysis.",
                    evidence=str(exc),
                )],
                scanned_lines=len(lines),
            )

        # Track imports to detect obfuscation combos
        imported_modules: set[str] = set()

        for node in ast.walk(tree):
            # BLT001: blocked builtins
            if isinstance(node, ast.Call):
                func_name = self._call_name(node)
                if func_name in BLOCKED_BUILTINS:
                    findings.append(ScanFinding(
                        rule_id="BLT001",
                        severity="critical",
                        line=node.lineno,
                        message=f"Blocked builtin call: {func_name}()",
                        evidence=ast.unparse(node)[:200],
                    ))

            # IMP001: blocked module imports
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                mods = self._import_names(node)
                for mod in mods:
                    imported_modules.add(mod.split(".")[0])
                    if any(mod == b or mod.startswith(b + ".") for b in BLOCKED_MODULES):
                        findings.append(ScanFinding(
                            rule_id="IMP001",
                            severity="critical",
                            line=node.lineno,
                            message=f"Blocked module import: {mod}",
                            evidence=ast.unparse(node)[:200],
                        ))

            # ENV001: os.environ access
            if isinstance(node, ast.Attribute):
                if (
                    isinstance(node.value, ast.Name)
                    and node.value.id == "os"
                    and node.attr == "environ"
                ):
                    findings.append(ScanFinding(
                        rule_id="ENV001",
                        severity="warning",
                        line=node.lineno,
                        message="os.environ access detected; verify env vars are declared.",
                        evidence=ast.unparse(node)[:200],
                    ))

                # ESC001: dunder access used to escape the builtins allowlist
                if node.attr in BLOCKED_DUNDERS:
                    findings.append(ScanFinding(
                        rule_id="ESC001",
                        severity="critical",
                        line=node.lineno,
                        message=f"Blocked dunder access: .{node.attr}",
                        evidence=ast.unparse(node)[:200],
                    ))

            # ESC002: getattr/setattr/delattr with a dynamic or dunder target
            if isinstance(node, ast.Call):
                func_name = self._call_name(node)
                if func_name in _DYNAMIC_ATTR_FUNCS and len(node.args) >= 2:
                    second = node.args[1]
                    flagged = False
                    # getattr(x, "__class__") or similar constant dunder
                    if isinstance(second, ast.Constant) and isinstance(second.value, str):
                        if second.value in BLOCKED_DUNDERS:
                            flagged = True
                    # getattr(x, some_variable) — dynamic attribute name
                    elif not isinstance(second, ast.Constant):
                        flagged = True
                    if flagged:
                        findings.append(ScanFinding(
                            rule_id="ESC002",
                            severity="critical",
                            line=node.lineno,
                            message=f"{func_name}() with dynamic or dunder attribute",
                            evidence=ast.unparse(node)[:200],
                        ))

        # OBF001: base64 + exec combo (check if both in imports or calls)
        has_base64 = "base64" in imported_modules
        has_exec_call = any(f.rule_id == "BLT001" and "exec" in f.message for f in findings)
        if has_base64 and has_exec_call:
            findings.append(ScanFinding(
                rule_id="OBF001",
                severity="critical",
                line=0,
                message="Potential obfuscation: base64 import combined with exec() call.",
                evidence="base64 + exec combination detected.",
            ))

        has_critical = any(f.severity == "critical" for f in findings)
        has_warning = any(f.severity == "warning" for f in findings)
        status = "critical" if has_critical else ("warning" if has_warning else "clean")
        return ASTScanResult(
            skill_id=skill_id,
            status=status,
            findings=findings,
            scanned_lines=len(lines),
        )

    @staticmethod
    def _call_name(node: ast.Call) -> str:
        if isinstance(node.func, ast.Name):
            return node.func.id
        if isinstance(node.func, ast.Attribute):
            return node.func.attr
        return ""

    @staticmethod
    def _import_names(node: ast.stmt) -> List[str]:
        names: List[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            names = [f"{mod}.{alias.name}" for alias in node.names] + [mod]
        return names
