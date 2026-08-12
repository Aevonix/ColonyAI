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
import importlib.util
import json
import pathlib
import re
import sys

import pytest
from jsonschema import Draft202012Validator

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


@pytest.fixture(scope="module")
def plugin_module():
    name = "colony_hermes_hostworker_agreement_test"
    spec = importlib.util.spec_from_file_location(
        name,
        _PLUGIN_INIT,
        submodule_search_locations=[str(_PLUGIN_INIT.parent)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


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


def test_plugin_action_intent_tool_names_match(plugin_module):
    """The plugin consumes the catalog name set instead of reauthoring it."""

    names = plugin_module._ACTION_INTENT_TOOL_NAMES
    assert set(names) == set(endpoint.ACTION_TOOL_NAMES)
    assert set(names) == set(hw_catalog.ACTION_TOOL_NAMES)


def test_plugin_action_schemas_match_hostworker_catalog(plugin_module):
    advertised = tuple(
        schema for schema in plugin_module._TOOL_SCHEMAS
        if schema["name"] in hw_catalog.ACTION_TOOL_NAMES
    )
    assert advertised == hw_catalog.ACTION_MODEL_TOOL_SCHEMAS
    for schema in advertised:
        Draft202012Validator.check_schema(schema["parameters"])


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


def _assert_boundary(schema, tool, accepted, rejected):
    advertised = Draft202012Validator(schema)
    assert not list(advertised.iter_errors(accepted))
    assert list(advertised.iter_errors(rejected))
    assert endpoint._validate_args(tool, accepted) == accepted
    assert hw_catalog.validate_tool_args(tool, accepted) == accepted
    with pytest.raises(endpoint.GovernedActionValidationError):
        endpoint._validate_args(tool, rejected)
    with pytest.raises(hw_contract.GovernedContractError):
        hw_catalog.validate_tool_args(tool, rejected)


def test_advertised_bounds_match_both_independent_validators(plugin_module):
    schemas = {schema["name"]: schema for schema in plugin_module._TOOL_SCHEMAS}
    properties = {
        name: schema["parameters"]["properties"]
        for name, schema in schemas.items()
    }
    constant_pairs = (
        (
            "GOVERNED_COMMITMENT_DESCRIPTION_MAX_CHARS",
            "COMMITMENT_DESCRIPTION_MAX_CHARS",
        ),
        (
            "GOVERNED_COMMITMENT_DUE_AT_MAX_CHARS",
            "COMMITMENT_DUE_AT_MAX_CHARS",
        ),
        ("GOVERNED_INSIGHT_CONTENT_MAX_CHARS", "INSIGHT_CONTENT_MAX_CHARS"),
        ("GOVERNED_RESEARCH_TOPIC_MAX_CHARS", "RESEARCH_TOPIC_MAX_CHARS"),
        ("GOVERNED_FREEFORM_REASON_MAX_CHARS", "FREEFORM_REASON_MAX_CHARS"),
        ("GOVERNED_IDENTIFIER_MAX_CHARS", "IDENTIFIER_MAX_CHARS"),
        ("GOVERNED_DETAILS_MAX_NODES", "BOUNDED_JSON_MAX_NODES"),
        ("GOVERNED_DETAILS_MAX_DEPTH", "BOUNDED_JSON_MAX_DEPTH"),
        (
            "GOVERNED_DETAILS_STRING_MAX_CHARS",
            "BOUNDED_JSON_STRING_MAX_CHARS",
        ),
        (
            "GOVERNED_DETAILS_KEY_MAX_CHARS",
            "BOUNDED_JSON_KEY_MAX_CHARS",
        ),
        ("GOVERNED_DETAILS_INTEGER_MAX", "BOUNDED_JSON_INTEGER_MAX"),
    )
    for endpoint_name, catalog_name in constant_pairs:
        assert getattr(endpoint, endpoint_name) == getattr(hw_catalog, catalog_name)

    text_specs = (
        (
            "colony_create_commitment", {}, "description", "d",
            hw_catalog.COMMITMENT_DESCRIPTION_MAX_CHARS,
        ),
        (
            "colony_create_commitment", {"description": "d"}, "due_at", "t",
            hw_catalog.COMMITMENT_DUE_AT_MAX_CHARS,
        ),
        (
            "colony_record_insight", {"insight_type": "fact"}, "content", "c",
            hw_catalog.INSIGHT_CONTENT_MAX_CHARS,
        ),
        (
            "colony_research", {}, "topic", "r",
            hw_catalog.RESEARCH_TOPIC_MAX_CHARS,
        ),
        (
            "colony_resolve_commitment", {"commitment_id": "c"}, "reason", "r",
            hw_catalog.FREEFORM_REASON_MAX_CHARS,
        ),
        (
            "colony_task_snooze", {"task_id": "t"}, "reason", "r",
            hw_catalog.FREEFORM_REASON_MAX_CHARS,
        ),
    )
    cases = []
    for tool, base, field, character, maximum in text_specs:
        field_schema = properties[tool][field]
        assert field_schema["maxLength"] == maximum
        cases.append((
            tool,
            {**base, field: character * maximum},
            {**base, field: character * (maximum + 1)},
        ))
        cases.append((
            tool,
            {**base, field: "" if "minLength" not in field_schema else character},
            {**base, field: character + "\x00"},
        ))
        if "minLength" in field_schema:
            cases.append((
                tool,
                {**base, field: character},
                {**base, field: " \n"},
            ))

    identifier_fields = (
        ("colony_get_initiative", {}, "initiative_id"),
        ("colony_initiative_feedback", {"action": "actioned"}, "initiative_id"),
        ("colony_resolve_commitment", {}, "commitment_id"),
        ("colony_task_complete", {}, "task_id"),
        ("colony_task_dismiss", {}, "task_id"),
        ("colony_task_snooze", {}, "task_id"),
    )
    identifier_schema = hw_catalog.identifier_model_schema()
    assert endpoint.GOVERNED_IDENTIFIER_PATTERN == identifier_schema["pattern"]
    # JSON Schema applies ``pattern`` as a search.  The exact-end assertion
    # must still reject the final-newline case that a terminal ``$`` admits.
    assert re.search(identifier_schema["pattern"], "identifier\n") is None
    for tool, base, field in identifier_fields:
        assert properties[tool][field] == identifier_schema
        if tool == "colony_get_initiative":
            continue  # Read tool: it does not cross the two governed validators.
        accepted = {**base, field: "i" * hw_catalog.IDENTIFIER_MAX_CHARS}
        cases.extend((
            (
                tool, accepted,
                {**base, field: "i" * (hw_catalog.IDENTIFIER_MAX_CHARS + 1)},
            ),
            (tool, accepted, {**base, field: "bad id"}),
            (tool, accepted, {**base, field: "identifier\n"}),
        ))

    parameters = schemas["colony_initiative_feedback"]["parameters"]
    details_schema = parameters["properties"]["details"]
    assert details_schema["type"] == "object"
    assert "$ref" not in details_schema
    assert details_schema["maxProperties"] == hw_catalog.BOUNDED_JSON_MAX_NODES - 1
    definitions = parameters["$defs"]
    assert details_schema["propertyNames"] == {
        "type": "string",
        "maxLength": hw_catalog.BOUNDED_JSON_KEY_MAX_CHARS,
        "pattern": hw_catalog.IDENTIFIER_RE.pattern,
    }
    assert re.search(
        details_schema["propertyNames"]["pattern"], "key\n",
    ) is None
    description = details_schema["description"]
    for bound in (
        hw_catalog.BOUNDED_JSON_MAX_NODES,
        hw_catalog.BOUNDED_JSON_MAX_DEPTH,
        hw_catalog.BOUNDED_JSON_STRING_MAX_CHARS,
        hw_catalog.BOUNDED_JSON_KEY_MAX_CHARS,
    ):
        assert str(bound) in description
    for depth in range(1, hw_catalog.BOUNDED_JSON_MAX_DEPTH + 1):
        variants = definitions[f"detailsValue{depth}"]["anyOf"]
        text = next(item for item in variants if item["type"] == "string")
        assert text["maxLength"] == hw_catalog.BOUNDED_JSON_STRING_MAX_CHARS
        number = next(item for item in variants if item["type"] == "number")
        assert number["minimum"] == -hw_catalog.BOUNDED_JSON_INTEGER_MAX
        assert number["maximum"] == hw_catalog.BOUNDED_JSON_INTEGER_MAX
        if depth < hw_catalog.BOUNDED_JSON_MAX_DEPTH:
            local_maximum = hw_catalog.BOUNDED_JSON_MAX_NODES - depth - 1
            assert next(
                item for item in variants if item["type"] == "array"
            )["maxItems"] == local_maximum
            assert next(
                item for item in variants if item["type"] == "object"
            )["maxProperties"] == local_maximum
    terminal = definitions[f"detailsValue{hw_catalog.BOUNDED_JSON_MAX_DEPTH}"]["anyOf"]
    assert next(item for item in terminal if item["type"] == "array")["maxItems"] == 0
    assert next(item for item in terminal if item["type"] == "object")["maxProperties"] == 0

    base = {"initiative_id": "i", "action": "actioned"}
    feedback = lambda value: {**base, "details": value}
    string_max = hw_catalog.BOUNDED_JSON_STRING_MAX_CHARS
    key_max = hw_catalog.BOUNDED_JSON_KEY_MAX_CHARS
    children = hw_catalog.BOUNDED_JSON_MAX_NODES - 2
    integer_max = hw_catalog.BOUNDED_JSON_INTEGER_MAX

    def nested_lists(levels):
        value = None
        for _ in range(levels):
            value = [value]
        return value

    cases.extend((
        (
            "colony_initiative_feedback",
            feedback({"v": "s" * string_max}),
            feedback({"v": "s" * (string_max + 1)}),
        ),
        (
            "colony_initiative_feedback",
            feedback({"v": ""}),
            feedback({"v": "s\x00"}),
        ),
        (
            "colony_initiative_feedback",
            feedback({"k" * key_max: None}),
            feedback({"k" * (key_max + 1): None}),
        ),
        (
            "colony_initiative_feedback",
            feedback({"valid": None}),
            feedback({"bad key": None}),
        ),
        (
            "colony_initiative_feedback",
            feedback({"valid": None}),
            feedback({"bad\n": None}),
        ),
        (
            "colony_initiative_feedback",
            feedback({"items": [None] * children}),
            feedback({"items": [None] * (children + 1)}),
        ),
        (
            "colony_initiative_feedback",
            feedback({"number": integer_max}),
            feedback({"number": integer_max + 1}),
        ),
        (
            "colony_initiative_feedback",
            feedback({
                "nested": nested_lists(hw_catalog.BOUNDED_JSON_MAX_DEPTH - 1),
            }),
            feedback({
                "nested": nested_lists(hw_catalog.BOUNDED_JSON_MAX_DEPTH),
            }),
        ),
    ))
    for tool, accepted, rejected in cases:
        _assert_boundary(schemas[tool]["parameters"], tool, accepted, rejected)


def test_aggregate_details_limit_is_advertised_and_prevalidated(plugin_module):
    schema = next(
        item for item in plugin_module._TOOL_SCHEMAS
        if item["name"] == "colony_initiative_feedback"
    )["parameters"]
    details = schema["properties"]["details"]
    assert (
        f"at most {hw_catalog.BOUNDED_JSON_MAX_NODES} total values"
        in details["description"]
    )
    accepted = {
        "initiative_id": "initiative-1",
        "action": "actioned",
        "details": {"a": [None] * 254, "b": [None] * 255},
    }
    invalid = {
        "initiative_id": "initiative-1",
        "action": "actioned",
        "details": {"a": [None] * 300, "b": [None] * 300},
    }
    assert endpoint._validate_args("colony_initiative_feedback", accepted) == accepted
    assert hw_catalog.validate_tool_args(
        "colony_initiative_feedback", accepted,
    ) == accepted
    with pytest.raises(endpoint.GovernedActionValidationError, match="too complex"):
        endpoint._validate_args("colony_initiative_feedback", invalid)
    with pytest.raises(hw_contract.GovernedContractError, match="too complex"):
        hw_catalog.validate_tool_args("colony_initiative_feedback", invalid)
    # Standard JSON Schema has no aggregate descendant-node counter.  The
    # plugin closes that portable-schema limitation by invoking the catalog's
    # exact validator before it constructs or submits an intent.
    with pytest.raises(ValueError, match="too complex"):
        plugin_module.HermesToolActionIntentV1.build(
            tool_name="colony_initiative_feedback",
            args=invalid,
            context={},
        )


@pytest.mark.parametrize(("tool", "args"), (
    (
        "colony_initiative_feedback",
        {
            "initiative_id": "initiative-1",
            "action": "actioned",
            "details": {"number": float("nan")},
        },
    ),
    (
        "colony_record_insight",
        {
            "content": "fact",
            "insight_type": "fact",
            "confidence": float("nan"),
        },
    ),
))
def test_non_json_numbers_fail_before_intent_serialization(
    plugin_module, tool, args,
):
    # NaN is outside the JSON data model, so JSON Schema has no portable
    # keyword for it.  Both independent validators and the plugin's catalog
    # prevalidation must reject it before canonical JSON is constructed.
    with pytest.raises(endpoint.GovernedActionValidationError):
        endpoint._validate_args(tool, args)
    with pytest.raises(hw_contract.GovernedContractError):
        hw_catalog.validate_tool_args(tool, args)
    with pytest.raises(ValueError):
        plugin_module.HermesToolActionIntentV1.build(
            tool_name=tool, args=args, context={},
        )


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


@pytest.mark.asyncio
async def test_create_commitment_default_matches_advertised_execution(
    plugin_module,
):
    class CapturingCommitments:
        def __init__(self):
            self.created = None

        def create(self, **kwargs):
            self.created = kwargs
            return {"id": "commitment-1", "status": "pending"}

    advertised = next(
        schema for schema in plugin_module._TOOL_SCHEMAS
        if schema["name"] == "colony_create_commitment"
    )["parameters"]["properties"]["priority"]["default"]
    assert advertised == hw_catalog.COMMITMENT_PRIORITY_DEFAULT
    assert advertised == endpoint.GOVERNED_COMMITMENT_PRIORITY_DEFAULT

    commitments = CapturingCommitments()
    executor = endpoint.ColonySubsystemActionExecutor(commitments=commitments)
    args = endpoint._validate_args(
        "colony_create_commitment", {"description": "Follow up"},
    )
    assert "priority" not in args
    await executor.perform(
        {"tool_name": "colony_create_commitment", "args": args},
        "owner",
    )
    assert commitments.created["priority"] == advertised


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
