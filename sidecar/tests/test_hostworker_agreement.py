"""Cross-implementation agreement: endpoint vs colony_hostworker.

DELIBERATE REDUNDANCY — DO NOT "UNIFY" THESE IMPLEMENTATIONS.

``colony_sidecar.governed_actions`` (the endpoint) and ``colony_hostworker``
(the stateless host-worker core) each keep their OWN independent validator and
digest implementation for the same wire contract.  That redundancy already
caught a real incompatibility (the ASCII/UTF-8 canonical-JSON digest split),
so the fix for a disagreement surfaced here is to decide which behavior the
contract intends and fix the diverging side — NEVER to make one import the
other.  This module is the tripwire: it replays shared golden vectors and a
battery of systematic mutations through BOTH implementations and fails if
they ever disagree on a digest, an acceptance, or a rejection.

It also enforces the independence itself: the sidecar package must not import
``colony_hostworker`` and ``colony_hostworker`` must not import the sidecar
(or any server framework).
"""

import ast
import json
import pathlib
import sys

import pytest

from colony_sidecar import governed_actions as endpoint

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_HOSTWORKER_DIR = _REPO_ROOT / "hostworker"
_VECTORS_PATH = _HOSTWORKER_DIR / "tests" / "vectors" / "golden_vectors.json"
_PLUGIN_INIT = _REPO_ROOT / "plugins" / "hermes-plugin" / "__init__.py"

if str(_HOSTWORKER_DIR) not in sys.path:
    sys.path.insert(0, str(_HOSTWORKER_DIR))

import colony_hostworker as hostworker  # noqa: E402
from colony_hostworker import catalog as hw_catalog  # noqa: E402
from colony_hostworker import contract as hw_contract  # noqa: E402


@pytest.fixture(scope="module")
def vectors() -> dict:
    with open(_VECTORS_PATH, encoding="utf-8") as handle:
        data = json.load(handle)
    assert data["schema"] == "ColonyHostWorkerGoldenVectorsV1"
    return data


def test_hostworker_package_is_present():
    assert _HOSTWORKER_DIR.is_dir(), (
        "colony_hostworker distribution missing at %s" % _HOSTWORKER_DIR
    )
    assert _VECTORS_PATH.is_file(), "shared golden vectors missing"


# ---------------------------------------------------------------- digests


def test_both_digest_conventions_agree_on_golden_vectors(vectors):
    """Byte-identical canonical JSON and digests, both conventions.

    If this fails, one side changed a serialization option (sort order,
    separators, escaping, allow_nan) and every digest that side produces is
    now incompatible with durable ledgers on the other side.
    """

    for vector in vectors["digests"]:
        value = vector["value"]
        # Endpoint (ASCII + UTF-8 wire helper).
        assert endpoint.canonical_json(value) == vector["canonical_ascii"]
        assert endpoint.sha256_json(value) == vector["sha256_ascii"]
        assert endpoint._wire_sha256_json(value) == vector["sha256_utf8"]
        # Host-worker core.
        assert (
            hw_contract.canonical_json_ascii(value)
            == vector["canonical_ascii"]
        )
        assert hw_contract.canonical_json_utf8(value) == vector["canonical_utf8"]
        assert hw_contract.sha256_json_ascii(value) == vector["sha256_ascii"]
        assert hw_contract.sha256_json_utf8(value) == vector["sha256_utf8"]


# ---------------------------------------------------------------- catalog


def test_catalogs_enumerate_the_same_tools(vectors):
    assert set(endpoint.ACTION_TOOL_NAMES) == set(hw_catalog.ACTION_TOOL_NAMES)
    assert sorted(endpoint.ACTION_TOOL_NAMES) == vectors["action_tool_names"]


def test_plugin_action_intent_tool_names_match():
    """The Hermes plugin's governed-intent tool list is the third copy of the
    catalog's name set; extract it from source (the plugin package imports
    httpx at module scope) and pin it to the other two."""

    tree = ast.parse(_PLUGIN_INIT.read_text(encoding="utf-8"))
    names = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and getattr(
            node.target, "id", ""
        ) == "_ACTION_INTENT_TOOL_NAMES":
            names = ast.literal_eval(node.value)
        elif isinstance(node, ast.Assign) and any(
            getattr(target, "id", "") == "_ACTION_INTENT_TOOL_NAMES"
            for target in node.targets
        ):
            names = ast.literal_eval(node.value)
    assert names is not None, "_ACTION_INTENT_TOOL_NAMES not found in plugin"
    assert set(names) == set(endpoint.ACTION_TOOL_NAMES)
    assert set(names) == set(hw_catalog.ACTION_TOOL_NAMES)


def test_valid_args_accepted_identically(vectors):
    for vector in vectors["valid_args"]:
        tool, args = vector["tool_name"], vector["args"]
        from_endpoint = endpoint._validate_args(tool, args)
        from_hostworker = hw_catalog.validate_tool_args(tool, args)
        assert from_endpoint == from_hostworker == vector["normalized"], vector
        assert (
            endpoint.sha256_json(from_endpoint)
            == hw_contract.sha256_json_ascii(from_hostworker)
            == vector["args_sha256_ascii"]
        )


def test_invalid_args_rejected_identically(vectors):
    for vector in vectors["invalid_args"]:
        with pytest.raises(endpoint.GovernedActionValidationError):
            endpoint._validate_args(vector["tool_name"], vector["args"])
        with pytest.raises(hw_contract.GovernedContractError):
            hw_catalog.validate_tool_args(vector["tool_name"], vector["args"])


def _mutations(args):
    """Systematic single-field mutations of a valid args document."""

    yield {**args, "unknown_extra_field": 1}
    for key in args:
        without = {k: v for k, v in args.items() if k != key}
        yield without
        yield {**args, key: None}
        yield {**args, key: [args[key]]}
        yield {**args, key: True}
        if isinstance(args[key], str):
            yield {**args, key: args[key] + " "}
            yield {**args, key: args[key] + "\x00"}
        if isinstance(args[key], int) and not isinstance(args[key], bool):
            yield {**args, key: args[key] + 10_000}
    yield "not an object"
    yield [list(args.items())]


def test_mutation_battery_verdicts_agree(vectors):
    """Both validators must return the SAME verdict for every mutation.

    This is the drift tripwire: it does not pin what the verdict is, only
    that the two independent implementations never disagree.  A divergence
    means one catalog copy changed without the other.
    """

    checked = 0
    for vector in vectors["valid_args"]:
        tool = vector["tool_name"]
        for mutated in _mutations(vector["args"]):
            try:
                from_endpoint = endpoint._validate_args(tool, mutated)
                endpoint_accepts = True
            except endpoint.GovernedActionValidationError:
                endpoint_accepts = False
            try:
                from_hostworker = hw_catalog.validate_tool_args(tool, mutated)
                hostworker_accepts = True
            except hw_contract.GovernedContractError:
                hostworker_accepts = False
            assert endpoint_accepts == hostworker_accepts, (tool, mutated)
            if endpoint_accepts:
                assert from_endpoint == from_hostworker, (tool, mutated)
                assert endpoint.sha256_json(
                    from_endpoint
                ) == hw_contract.sha256_json_ascii(from_hostworker)
            checked += 1
    assert checked > 200


# ---------------------------------------------------------------- execution


def test_golden_execution_request_agrees_end_to_end(vectors):
    """The endpoint accepts the golden execution request byte-for-byte and
    the host-worker core reproduces every digest inside it."""

    golden = vectors["execution_request"]
    document = golden["document"]
    raw = golden["raw_utf8"].encode("utf-8")
    normalized = endpoint.parse_execution_request(
        raw, path_action_id=golden["path_action_id"]
    )
    assert normalized == document

    # Host-worker core recomputes the ASCII-convention digests...
    assert (
        hw_contract.sha256_json_ascii(document["args"])
        == document["args_sha256"]
        == golden["args_sha256_ascii"]
    )
    # ...and the UTF-8-convention execution digest over the unsigned body.
    unsigned = {
        key: value
        for key, value in document.items()
        if key != "execution_digest"
    }
    assert (
        hw_contract.sha256_json_utf8(unsigned)
        == document["execution_digest"]
        == golden["execution_digest_utf8"]
    )
    # Both sides agree the two conventions are NOT interchangeable here: the
    # args carry non-ASCII text precisely so a convention swap cannot hide.
    assert hw_contract.sha256_json_utf8(document["args"]) != document[
        "args_sha256"
    ]
    assert hw_contract.sha256_json_ascii(unsigned) != document[
        "execution_digest"
    ]
    # The endpoint's own helpers agree with the host-worker core on both.
    assert endpoint.sha256_json(document["args"]) == document["args_sha256"]
    assert endpoint._wire_sha256_json(unsigned) == document["execution_digest"]

    # The intent embedded in the golden request revalidates in the host-worker
    # core with identical digests.
    intent = hostworker.HermesToolActionIntentV1.build(
        tool_name=document["tool_name"],
        args=document["args"],
        context={
            "api_request_id": "req-1",
            "authority_lane": "owner",
            "contact_id": "contact-1",
            "platform": "whatsapp",
            "sender_id": "owner:1",
            "session_id": "sess-1",
            "task_id": "",
            "tool_call_id": "call-1",
            "turn_id": "turn-1",
        },
    )
    assert intent.intent_id == document["intent_id"]
    assert intent.intent_digest == document["intent_digest"]
    assert intent.args_sha256 == document["args_sha256"]


def test_tampered_execution_request_rejected_by_endpoint(vectors):
    golden = vectors["execution_request"]
    document = json.loads(golden["raw_utf8"])
    document["args"] = {**document["args"], "priority": 61}
    raw = json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    with pytest.raises(endpoint.GovernedActionValidationError):
        endpoint.parse_execution_request(
            raw, path_action_id=golden["path_action_id"]
        )


# ---------------------------------------------------------------- independence


def test_sidecar_never_imports_colony_hostworker():
    """The endpoint keeps its own validator; importing colony_hostworker from
    the sidecar would collapse the deliberate redundancy this suite protects.
    See the module docstring before "fixing" a failure here."""

    package_root = pathlib.Path(endpoint.__file__).resolve().parent
    offenders = []
    for source_file in package_root.rglob("*.py"):
        tree = ast.parse(source_file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                if any(
                    alias.name.split(".")[0] == "colony_hostworker"
                    for alias in node.names
                ):
                    offenders.append(source_file)
            elif isinstance(node, ast.ImportFrom):
                if (node.module or "").split(".")[0] == "colony_hostworker":
                    offenders.append(source_file)
    assert not offenders, (
        "colony_sidecar must never import colony_hostworker: %s" % offenders
    )


def test_hostworker_never_imports_the_sidecar_or_a_server():
    package_root = _HOSTWORKER_DIR / "colony_hostworker"
    forbidden = {"colony_sidecar", "fastapi", "httpx", "pydantic", "uvicorn"}
    offenders = []
    for source_file in package_root.rglob("*.py"):
        tree = ast.parse(source_file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                if any(
                    alias.name.split(".")[0] in forbidden
                    for alias in node.names
                ):
                    offenders.append(source_file)
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and (node.module or "").split(".")[
                    0
                ] in forbidden:
                    offenders.append(source_file)
    assert not offenders, (
        "colony_hostworker must stay stdlib-only: %s" % offenders
    )


def test_independence_rule_is_documented_in_the_contract():
    contract_source = (
        _HOSTWORKER_DIR / "colony_hostworker" / "contract.py"
    ).read_text(encoding="utf-8")
    module_docstring = ast.get_docstring(ast.parse(contract_source)) or ""
    flattened = " ".join(module_docstring.split())
    assert "MUST KEEP ITS OWN INDEPENDENT VALIDATOR" in flattened
    assert "governed_actions.py" in flattened
