"""HermesToolActionIntentV1 validation stays byte-compatible with the wire."""

import pytest

from colony_hostworker.contract import sha256_json_ascii
from colony_hostworker.intent import (
    HermesActionIntentError,
    HermesToolActionIntentV1,
)

CONTEXT = {
    "api_request_id": "req-1",
    "authority_lane": "owner",
    "contact_id": "contact-1",
    "platform": "whatsapp",
    "sender_id": "owner:1",
    "session_id": "sess-1",
    "task_id": "",
    "tool_call_id": "call-1",
    "turn_id": "turn-1",
}


def build_intent(**overrides):
    kwargs = {
        "tool_name": "colony_create_commitment",
        "args": {"description": "Review café ☕ notes", "priority": 60},
        "context": CONTEXT,
    }
    kwargs.update(overrides)
    return HermesToolActionIntentV1.build(**kwargs)


def test_wire_schema_string_is_exactly_the_public_name():
    document = build_intent().to_dict()
    assert document["schema"] == "HermesToolActionIntentV1"


def test_round_trip_build_and_from_mapping():
    intent = build_intent()
    parsed = HermesToolActionIntentV1.from_mapping(intent.to_dict())
    assert parsed == intent
    assert parsed.to_dict() == intent.to_dict()


def test_golden_execution_intent_revalidates(golden_vectors):
    """Rebuilding from the same call identity reproduces the exact digests
    the endpoint accepted when the golden execution request was generated."""

    document = golden_vectors["execution_request"]["document"]
    intent = build_intent()
    assert intent.intent_id == document["intent_id"]
    assert intent.intent_digest == document["intent_digest"]
    assert intent.args_sha256 == document["args_sha256"]
    assert intent.args == document["args"]


def test_digest_convention_is_ascii(golden_vectors):
    intent = build_intent()
    assert intent.args_sha256 == sha256_json_ascii(intent.args)
    # Non-ASCII args make the convention observable: the UTF-8 digest of the
    # same args differs, so a convention swap cannot pass this test.
    from colony_hostworker.contract import sha256_json_utf8

    assert intent.args_sha256 != sha256_json_utf8(intent.args)


def test_intent_id_derivation_is_pinned():
    intent = build_intent()
    assert intent.intent_id == "hti_" + intent.idempotency_key[:32]


def test_tampered_args_refused():
    document = build_intent().to_dict()
    document["args"] = {"description": "different", "priority": 60}
    with pytest.raises(HermesActionIntentError):
        HermesToolActionIntentV1.from_mapping(document)


def test_tampered_intent_digest_refused():
    document = build_intent().to_dict()
    document["intent_digest"] = "0" * 64
    with pytest.raises(HermesActionIntentError):
        HermesToolActionIntentV1.from_mapping(document)


def test_foreign_schema_refused():
    document = build_intent().to_dict()
    document["schema"] = "HermesToolActionIntentV2"
    with pytest.raises(HermesActionIntentError):
        HermesToolActionIntentV1.from_mapping(document)


def test_extra_field_refused():
    document = build_intent().to_dict()
    document["note"] = "extra"
    with pytest.raises(HermesActionIntentError):
        HermesToolActionIntentV1.from_mapping(document)


def test_missing_context_field_refused():
    document = build_intent().to_dict()
    del document["context"]["platform"]
    with pytest.raises(HermesActionIntentError):
        HermesToolActionIntentV1.from_mapping(document)


def test_ungoverned_tool_refused():
    document = build_intent().to_dict()
    document["tool_name"] = "colony_send_message"
    with pytest.raises(HermesActionIntentError):
        HermesToolActionIntentV1.from_mapping(document)


def test_idempotency_key_binds_call_identity():
    intent = build_intent()
    other = build_intent(
        context={**CONTEXT, "tool_call_id": "call-2"},
    )
    assert intent.idempotency_key != other.idempotency_key
    # Fields outside HermesActionCallV1 (e.g. contact_id) do not change the
    # idempotency key but do change the context digest and intent digest.
    rebound = build_intent(context={**CONTEXT, "contact_id": "contact-2"})
    assert rebound.idempotency_key == intent.idempotency_key
    assert rebound.context_sha256 != intent.context_sha256
    assert rebound.intent_digest != intent.intent_digest


def test_approval_display_delegates_to_catalog():
    display = build_intent().approval_display()
    assert set(display) == {"summary", "target", "risk"}
    assert display["target"] == "Private Colony commitment ledger"
