"""Client boundary: credential loading, loopback pinning, strict exchange."""

import io
import json
import os
import uuid

import pytest

from colony_hostworker import contract
from colony_hostworker._private_io import strict_json_bytes
from colony_hostworker.client import (
    ClientCredential,
    GovernedActionClient,
    GovernedActionClientError,
    NoRedirectHandler,
    WORKER_PRINCIPAL,
)

SECRET = "S" * 48


def write_credential(path, document, mode=0o600):
    path.write_text(json.dumps(document))
    os.chmod(path, mode)
    return str(path)


def credential_document(**overrides):
    document = {
        "version": 1,
        "principal": WORKER_PRINCIPAL,
        "credential_id": "host-worker-key-1",
        "secret": SECRET,
    }
    document.update(overrides)
    return document


def test_credential_loads_from_private_file(tmp_path):
    path = write_credential(tmp_path / "credential.json", credential_document())
    credential = ClientCredential.load(path)
    assert credential.principal == WORKER_PRINCIPAL
    assert credential.credential_id == "host-worker-key-1"
    assert credential.secret == SECRET
    assert SECRET not in repr(credential)


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o660, 0o700])
def test_credential_refuses_permissive_modes(tmp_path, mode):
    path = write_credential(tmp_path / "credential.json", credential_document(), mode)
    with pytest.raises(GovernedActionClientError):
        ClientCredential.load(path)


def test_credential_refuses_symlink(tmp_path):
    real = tmp_path / "real.json"
    write_credential(real, credential_document())
    link = tmp_path / "link.json"
    link.symlink_to(real)
    with pytest.raises(GovernedActionClientError):
        ClientCredential.load(str(link))


@pytest.mark.parametrize(
    "overrides",
    [
        {"principal": "someone-else"},
        {"version": 2},
        {"version": True},
        {"secret": "short"},
        {"secret": "x" * 600},
        {"secret": "bad secret with spaces" + "x" * 32},
        {"credential_id": "bad id"},
        {"extra": "field"},
    ],
)
def test_credential_refuses_invalid_documents(tmp_path, overrides):
    document = credential_document(**overrides)
    path = write_credential(tmp_path / "credential.json", document)
    with pytest.raises(GovernedActionClientError):
        ClientCredential.load(path)


def test_credential_refuses_missing_field(tmp_path):
    document = credential_document()
    del document["secret"]
    path = write_credential(tmp_path / "credential.json", document)
    with pytest.raises(GovernedActionClientError):
        ClientCredential.load(path)


# ----------------------------------------------------------------- origin


def make_credential(tmp_path):
    path = write_credential(tmp_path / "credential.json", credential_document())
    return ClientCredential.load(path)


@pytest.mark.parametrize(
    "origin",
    [
        "http://127.0.0.1:8123",
        "https://localhost",
        "http://[::1]:9999",
    ],
)
def test_loopback_origins_accepted(tmp_path, origin):
    client = GovernedActionClient(origin, make_credential(tmp_path))
    assert client.origin == origin


@pytest.mark.parametrize(
    "origin",
    [
        "http://192.0.2.1:8123",
        "http://example.com",
        "http://localhost/path",
        "http://localhost?query=1",
        "http://user:pw@127.0.0.1",
        "ftp://127.0.0.1",
        " http://127.0.0.1",
        "http://127.0.0.1:0",
        "",
    ],
)
def test_non_loopback_origins_refused(tmp_path, origin):
    with pytest.raises(GovernedActionClientError):
        GovernedActionClient(origin, make_credential(tmp_path))


def test_redirects_are_refused_not_followed():
    handler = NoRedirectHandler()
    assert (
        handler.redirect_request(None, None, 302, "Found", {}, "http://evil/")
        is None
    )


# --------------------------------------------------------------- exchange


class FakeResponse:
    def __init__(self, status=200, body=b"{}"):
        self.status = status
        self._body = io.BytesIO(body)

    def read(self, amount=-1):
        return self._body.read(amount)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeOpener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append((request, timeout))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def execution_request(**overrides):
    request = {
        "schema": contract.EXECUTION_REQUEST_SCHEMA,
        "version": 1,
        "action_id": str(uuid.uuid4()),
        "action_digest": "a" * 64,
        "intent_id": "hti_" + "b" * 32,
        "intent_digest": "c" * 64,
        "tool_name": "colony_create_commitment",
        "args": {"description": "hello"},
        "args_sha256": "d" * 64,
        "approval": {"schema": "x"},
        "execution_digest": "e" * 64,
    }
    request.update(overrides)
    return request


def make_client(tmp_path, opener):
    return GovernedActionClient(
        "http://127.0.0.1:8123", make_credential(tmp_path), opener=opener
    )


def test_execute_sends_exactly_one_canonical_put(tmp_path):
    opener = FakeOpener([FakeResponse(200, b'{"status": "completed"}')])
    client = make_client(tmp_path, opener)
    request = execution_request()
    result = client.execute(request)
    assert result == {"status": "completed"}
    assert len(opener.requests) == 1
    sent, timeout = opener.requests[0]
    assert sent.get_method() == "PUT"
    assert sent.full_url == "http://127.0.0.1:8123/v1/host/actions/" + request[
        "action_id"
    ]
    assert sent.data == contract.canonical_json_utf8(request).encode("utf-8")
    assert sent.get_header("Authorization") == "Bearer " + SECRET
    assert sent.get_header("X-colony-principal") == WORKER_PRINCIPAL
    assert timeout == pytest.approx(5.0)


def test_observe_sends_bodyless_get(tmp_path):
    opener = FakeOpener([FakeResponse(200, b'{"status": "executing"}')])
    client = make_client(tmp_path, opener)
    client.observe(execution_request())
    sent, _ = opener.requests[0]
    assert sent.get_method() == "GET"
    assert sent.data is None


def test_failed_put_is_never_retried(tmp_path):
    opener = FakeOpener([OSError("connection reset"), FakeResponse(200, b"{}")])
    client = make_client(tmp_path, opener)
    with pytest.raises(GovernedActionClientError):
        client.execute(execution_request())
    # One open() only: the second canned response must remain unconsumed.
    assert len(opener.requests) == 1
    assert len(opener.responses) == 1


@pytest.mark.parametrize("status", [201, 301, 302, 400, 401, 500])
def test_non_200_is_refused(tmp_path, status):
    opener = FakeOpener([FakeResponse(status, b"{}")])
    client = make_client(tmp_path, opener)
    with pytest.raises(GovernedActionClientError):
        client.execute(execution_request())


def test_oversized_response_is_refused(tmp_path):
    body = b'{"pad": "' + b"x" * contract.EXECUTION_RESULT_MAX_BYTES + b'"}'
    opener = FakeOpener([FakeResponse(200, body)])
    client = make_client(tmp_path, opener)
    with pytest.raises(GovernedActionClientError):
        client.observe(execution_request())


def test_oversized_request_is_refused_before_any_io(tmp_path):
    opener = FakeOpener([])
    client = make_client(tmp_path, opener)
    request = execution_request(
        args={"description": "y" * (contract.EXECUTION_REQUEST_MAX_BYTES + 10)}
    )
    with pytest.raises(GovernedActionClientError):
        client.execute(request)
    assert not opener.requests


@pytest.mark.parametrize(
    "mutate",
    [
        lambda request: request.pop("approval"),
        lambda request: request.update(extra=1),
        lambda request: request.update(schema="Other"),
        lambda request: request.update(version=2),
        lambda request: request.update(action_id="not-a-uuid"),
        lambda request: request.update(action_id="A" * 36),
    ],
)
def test_invalid_request_documents_are_refused(tmp_path, mutate):
    opener = FakeOpener([])
    client = make_client(tmp_path, opener)
    request = execution_request()
    mutate(request)
    with pytest.raises(GovernedActionClientError):
        client.execute(request)
    assert not opener.requests


def test_response_with_duplicate_keys_is_refused(tmp_path):
    opener = FakeOpener([FakeResponse(200, b'{"a": 1, "a": 2}')])
    client = make_client(tmp_path, opener)
    with pytest.raises(GovernedActionClientError):
        client.observe(execution_request())


@pytest.mark.parametrize("timeout", [0.0, 0.01, 31.0, float("nan"), True])
def test_invalid_timeouts_are_refused(tmp_path, timeout):
    with pytest.raises(GovernedActionClientError):
        GovernedActionClient(
            "http://127.0.0.1:8123", make_credential(tmp_path), timeout=timeout
        )


# ---------------------------------------------------------- strict reader


@pytest.mark.parametrize(
    "raw",
    [
        b'{"a": NaN}',
        b'{"a": Infinity}',
        b'{"a": 99999999999999999999999}',
        b'{"a": "\x01"}',
        b"[" * 20 + b"1" + b"]" * 20,
        b'"' + b"x" * 9000 + b'"',
        b"not json",
    ],
)
def test_strict_reader_refuses_unsafe_documents(raw):
    with pytest.raises(RuntimeError):
        strict_json_bytes(raw, maximum=64 * 1024)


def test_strict_reader_round_trips_safe_documents():
    value = {"key": ["value", 1, 2.5, None, True]}
    raw = json.dumps(value).encode("utf-8")
    assert strict_json_bytes(raw, maximum=1024) == value
