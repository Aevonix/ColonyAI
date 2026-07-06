"""Toolsmith engine: draft -> verify -> shadow -> graduate (Mind M1).

Each stage composes shipped infrastructure:
  draft     LLM router writes source + input schema + a self-contained test
  verify    the test runs inside the egress-none Docker sandbox (replay)
  shadow    verified tools are advertised as shadow (simulated + journaled)
  graduate  trust engine (domain "toolsmith") gates shadow -> live
  retire    unused or failing tools demote automatically
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from colony_sidecar.toolsmith.miner import ToolCandidate, ToolsmithMiner
from colony_sidecar.toolsmith.registry import Tool, ToolRegistry, ToolStatus

logger = logging.getLogger(__name__)

TRUST_DOMAIN = "toolsmith"
_VERDICT_RE = re.compile(r"@@VERDICT@@\s*(\{.*\})", re.DOTALL)
_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def toolsmith_enabled() -> bool:
    return os.environ.get(
        "COLONY_TOOLSMITH", "off").strip().lower() in ("shadow", "live")


def toolsmith_mode() -> str:
    m = os.environ.get("COLONY_TOOLSMITH", "off").strip().lower()
    return m if m in ("off", "shadow", "live") else "off"


def _shadow_min() -> int:
    try:
        return int(os.environ.get("COLONY_TOOLSMITH_SHADOW_MIN", "5"))
    except ValueError:
        return 5


_DRAFT_SYSTEM = """You write small, dependency-free Python tools for an agent.

Given a recurring procedure the agent performs, output ONE JSON object (no
prose) with exactly these keys:
  name          snake_case identifier, 3-48 chars, [a-z][a-z0-9_]*
  description   one sentence: what the tool does and when to call it
  input_schema  JSON-Schema "properties" object for the tool's arguments
  source_code   a Python module defining `def run(**kwargs) -> dict:`
  test_source   Python that imports nothing external, calls run() with
                representative inputs, and asserts on the result

Hard rules:
- Standard library only. No network, no file writes, no subprocess, no
  environment access. The tool runs in a locked-down sandbox with no egress.
- `run` must be pure and deterministic given its inputs.
- The test must exercise run() and raise AssertionError on wrong output.
- If the procedure cannot be expressed as a safe pure function, output
  {"name": null} and nothing else."""


class Toolsmith:
    def __init__(self, registry: ToolRegistry, *, miner: Optional[ToolsmithMiner] = None,
                 sandbox: Any = None, self_model: Any = None,
                 router: Any = None) -> None:
        self.registry = registry
        self.miner = miner or ToolsmithMiner(registry=registry)
        self._sandbox = sandbox
        self._self_model = self_model
        self._router = router

    # -- lazy wiring ------------------------------------------------------
    def _sb(self) -> Any:
        if self._sandbox is not None:
            return self._sandbox
        try:
            from colony_sidecar.api.routers import host
            return getattr(host, "_sandbox", None)
        except Exception:
            return None

    def _sm(self) -> Any:
        if self._self_model is not None:
            return self._self_model
        try:
            from colony_sidecar.api.routers import host
            return getattr(host, "_self_model", None)
        except Exception:
            return None

    def _llm(self) -> Any:
        if self._router is not None:
            return self._router
        try:
            from colony_sidecar.api.routers import host
            return getattr(host, "_llm_router", None)
        except Exception:
            return None

    def _journal(self, desc: str, *, decision: str, outcome: str = "",
                 ref: str = "") -> None:
        sm = self._sm()
        journal = getattr(sm, "journal", None) if sm is not None else None
        if journal is None:
            return
        try:
            journal.record(TRUST_DOMAIN, desc, decision=decision,
                           outcome=outcome, ref=ref)
        except Exception:
            logger.debug("toolsmith journal write failed", exc_info=True)

    # -- draft ------------------------------------------------------------
    async def draft(self, candidate: ToolCandidate) -> Optional[Tool]:
        router = self._llm()
        if router is None:
            return None
        samples = "\n".join(f"- {d}" for d in candidate.sample_descriptions[:5])
        prompt = (
            f"Recurring procedure in domain '{candidate.domain}', observed "
            f"{candidate.occurrences} times. Representative descriptions:\n"
            f"{samples}\n\nWrite the tool.")
        try:
            resp = await router.complete(
                [{"role": "system", "content": _DRAFT_SYSTEM},
                 {"role": "user", "content": prompt}],
                context={"task": "toolsmith_draft"})
            spec = self._parse_spec(getattr(resp, "content", "") or "")
        except Exception as exc:
            logger.warning("toolsmith draft LLM failed: %s", exc)
            return None
        if not spec or not spec.get("name"):
            return None
        tool = self.registry.create_draft(
            name=spec["name"], description=spec.get("description", "")[:300],
            source_code=spec["source_code"],
            input_schema=spec.get("input_schema") or {},
            test_source=spec.get("test_source", ""),
            origin_kind=candidate and "mined" or "requested",
            evidence=candidate.evidence if candidate else [])
        if tool is not None:
            self._journal(
                f"drafted tool {tool.name} from {candidate.occurrences} "
                f"occurrences", decision="acted", ref=tool.tool_id)
        return tool

    @staticmethod
    def _parse_spec(content: str) -> Optional[Dict[str, Any]]:
        m = _JSON_RE.search(content)
        if not m:
            return None
        try:
            spec = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
        if spec.get("name") in (None, "", "null"):
            return None
        for k in ("source_code", "test_source"):
            if not isinstance(spec.get(k), str) or not spec[k].strip():
                return None
        if not ToolRegistry.valid_name(spec["name"]):
            return None
        return spec

    # -- verify -----------------------------------------------------------
    async def verify(self, tool: Tool) -> Tuple[bool, Dict[str, Any]]:
        """Run the tool's test inside the sandbox. Verified -> shadow."""
        sandbox = self._sb()
        if sandbox is None:
            return False, {"reason": "sandbox_unavailable"}
        script = self._verify_script(tool)
        try:
            # owner-directed: this is an owner-sanctioned autonomy process
            # running a pure test in an egress-none container.
            res = sandbox.run(script, "python",
                              purpose=f"toolsmith verify {tool.name}",
                              owner_directed=True)
        except Exception as exc:
            return False, {"reason": f"sandbox_error: {exc}"}
        if not res.get("ran"):
            return False, {"reason": res.get("reason", "not_run"),
                           "detail": res.get("detail")}
        result = res.get("result") or {}
        stdout = result.get("stdout", "")
        verdict = self._parse_verdict(stdout)
        passed = bool(verdict.get("passed")) and result.get("exit_code") == 0
        detail = {"passed": passed, "exit_code": result.get("exit_code"),
                  "verdict": verdict, "stderr": (result.get("stderr") or "")[:500]}
        self.registry.set_status(
            tool.tool_id,
            ToolStatus.SHADOW if passed else ToolStatus.REJECTED,
            verify_detail=detail)
        self._journal(
            f"verify tool {tool.name}: {'passed' if passed else 'failed'}",
            decision="acted", outcome="success" if passed else "failure",
            ref=tool.tool_id)
        return passed, detail

    @staticmethod
    def _verify_script(tool: Tool) -> str:
        return (
            "import json, traceback\n"
            "_ok = True\n_err = None\n"
            "try:\n"
            + _indent(tool.source_code) + "\n"
            + _indent(tool.test_source) + "\n"
            "except AssertionError as e:\n"
            "    _ok = False; _err = 'assertion: ' + str(e)\n"
            "except Exception as e:\n"
            "    _ok = False; _err = type(e).__name__ + ': ' + str(e)\n"
            "print('@@VERDICT@@ ' + json.dumps({'passed': _ok, 'error': _err}))\n"
        )

    @staticmethod
    def _parse_verdict(stdout: str) -> Dict[str, Any]:
        m = _VERDICT_RE.search(stdout or "")
        if not m:
            return {"passed": False, "error": "no verdict emitted"}
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            return {"passed": False, "error": "verdict parse failed"}

    # -- shadow accumulation ----------------------------------------------
    async def verify_shadow_run(self, tool: Tool) -> Tuple[bool, Dict[str, Any]]:
        """Re-run a shadow tool's own test in the sandbox as one shadow run.
        A pass is a clean shadow outcome that builds toward graduation; a
        fail counts against it (and the retirement path catches repeats).
        Status is NOT changed here (only mine/verify/graduate move status)."""
        sandbox = self._sb()
        if sandbox is None:
            return False, {"reason": "sandbox_unavailable"}
        script = self._verify_script(tool)
        try:
            res = sandbox.run(script, "python",
                              purpose=f"toolsmith shadow {tool.name}",
                              owner_directed=True)
        except Exception as exc:
            self.record_shadow(tool.tool_id, success=False)
            return False, {"reason": f"sandbox_error: {exc}"}
        result = res.get("result") or {}
        passed = (bool(self._parse_verdict(result.get("stdout", "")).get("passed"))
                  and result.get("exit_code") == 0)
        self.record_shadow(tool.tool_id, success=passed)
        return passed, {"passed": passed}

    # -- shadow invocation ------------------------------------------------
    def record_shadow(self, tool_id: str, success: bool) -> None:
        self.registry.record_invocation(tool_id, success=success, shadow=True)
        sm = self._sm()
        if sm is not None:
            try:
                sm.record(TRUST_DOMAIN,
                          "success" if success else "failure", shadow=True)
            except Exception:
                logger.debug("toolsmith shadow record failed", exc_info=True)

    def record_live(self, tool_id: str, success: bool) -> None:
        self.registry.record_invocation(tool_id, success=success)
        sm = self._sm()
        if sm is not None:
            try:
                sm.record(TRUST_DOMAIN,
                          "success" if success else "failure",
                          violation=not success)
            except Exception:
                logger.debug("toolsmith live record failed", exc_info=True)

    # -- graduation -------------------------------------------------------
    def graduation_candidates(self) -> List[Tool]:
        """Shadow tools that have enough clean shadow runs to promote."""
        floor = _shadow_min()
        return [t for t in self.registry.list(status=ToolStatus.SHADOW)
                if t.shadow_runs >= floor and t.failures == 0]

    def trust_stage(self) -> str:
        sm = self._sm()
        trust = getattr(sm, "trust", None) if sm is not None else None
        if trust is None:
            return "shadow"
        try:
            return trust.stage(TRUST_DOMAIN, default="shadow")
        except Exception:
            return "shadow"

    def graduate(self, tool_id: str) -> bool:
        """Promote a shadow tool to live (called after owner approval, or
        automatically when the toolsmith domain is act_first)."""
        tool = self.registry.get(tool_id)
        if tool is None or tool.status != ToolStatus.SHADOW:
            return False
        self.registry.set_status(tool_id, ToolStatus.LIVE)
        self._journal(f"graduated tool {tool.name} to live",
                      decision="acted", outcome="success", ref=tool_id)
        return True

    def retire(self, tool_id: str, reason: str = "") -> bool:
        tool = self.registry.get(tool_id)
        if tool is None:
            return False
        self.registry.set_status(tool_id, ToolStatus.RETIRED)
        self._journal(f"retired tool {tool.name}: {reason}"[:200],
                      decision="acted", ref=tool_id)
        return True

    # -- live invocation (from the reasoning loop) ------------------------
    async def invoke_live(self, tool_id: str,
                          kwargs: Dict[str, Any]) -> Dict[str, Any]:
        """Run a graduated tool's run(**kwargs) inside the sandbox and return
        its result. Records the outcome against trust."""
        tool = self.registry.get(tool_id)
        if tool is None or tool.status != ToolStatus.LIVE:
            return {"error": "tool not live"}
        sandbox = self._sb()
        if sandbox is None:
            return {"error": "sandbox unavailable"}
        script = self._call_script(tool, kwargs)
        try:
            res = sandbox.run(script, "python",
                              purpose=f"toolsmith invoke {tool.name}",
                              owner_directed=True)
        except Exception as exc:
            self.record_live(tool_id, success=False)
            return {"error": f"sandbox_error: {exc}"}
        result = res.get("result") or {}
        out = self._parse_verdict(result.get("stdout", ""))
        success = bool(out.get("passed")) and result.get("exit_code") == 0
        self.record_live(tool_id, success=success)
        if not success:
            return {"error": out.get("error") or "tool failed",
                    "stderr": (result.get("stderr") or "")[:300]}
        return {"result": out.get("result")}

    @staticmethod
    def _call_script(tool: Tool, kwargs: Dict[str, Any]) -> str:
        return (
            "import json\n_ok=True\n_err=None\n_res=None\n"
            "try:\n"
            + _indent(tool.source_code) + "\n"
            + _indent(f"_res = run(**{json.dumps(kwargs)})") + "\n"
            "except Exception as e:\n"
            "    _ok=False; _err=type(e).__name__+': '+str(e)\n"
            "print('@@VERDICT@@ ' + json.dumps({'passed': _ok, "
            "'error': _err, 'result': _res}))\n"
        )

    def build_dynamic_provider(self):
        """Return a callable for ToolExecutor.set_dynamic_provider that maps
        every LIVE tool to its (openai_definition, async_handler)."""
        def provider() -> Dict[str, Any]:
            out: Dict[str, Any] = {}
            for tool in self.registry.list(status=ToolStatus.LIVE):
                definition = {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": {
                            "type": "object",
                            "properties": tool.input_schema or {},
                        },
                    },
                }

                def make_handler(tid: str):
                    async def _handler(args: Dict[str, Any]) -> str:
                        r = await self.invoke_live(tid, args or {})
                        return json.dumps(r)
                    return _handler

                out[tool.name] = (definition, make_handler(tool.tool_id))
            return out
        return provider

    def retirement_candidates(self) -> List[Tool]:
        """Live tools failing repeatedly, or shadow tools stuck failing."""
        out: List[Tool] = []
        for t in self.registry.list(status=ToolStatus.LIVE):
            if t.invocations >= 3 and t.failures >= max(2, t.invocations // 2):
                out.append(t)
        for t in self.registry.list(status=ToolStatus.SHADOW):
            if t.failures >= 3:
                out.append(t)
        return out


def _indent(code: str, spaces: int = 4) -> str:
    pad = " " * spaces
    return "\n".join(pad + line for line in code.splitlines())
