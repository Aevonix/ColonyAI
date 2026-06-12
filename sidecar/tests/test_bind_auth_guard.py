"""The sidecar must fail closed rather than serve an unauthenticated API on a
non-loopback interface (COLONY_API_KEY unset + bind 0.0.0.0/LAN = open to net)."""
import pytest

from colony_sidecar.cli import _guard_bind_auth, _is_loopback_host


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost", ""])
def test_loopback_recognized(host):
    assert _is_loopback_host(host) is True


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10", "10.0.0.1"])
def test_non_loopback_recognized(host):
    assert _is_loopback_host(host) is False


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_loopback_allowed_without_key(host, monkeypatch):
    monkeypatch.delenv("COLONY_API_KEY", raising=False)
    _guard_bind_auth(host)  # must not raise/exit


def test_non_loopback_with_key_allowed(monkeypatch):
    monkeypatch.setenv("COLONY_API_KEY", "secret")
    _guard_bind_auth("0.0.0.0")  # must not raise/exit


def test_non_loopback_without_key_fails_closed(monkeypatch):
    monkeypatch.delenv("COLONY_API_KEY", raising=False)
    monkeypatch.delenv("COLONY_ALLOW_OPEN_BIND", raising=False)
    with pytest.raises(SystemExit) as exc:
        _guard_bind_auth("0.0.0.0")
    assert exc.value.code == 2


def test_explicit_override_allows_open_bind(monkeypatch):
    monkeypatch.delenv("COLONY_API_KEY", raising=False)
    monkeypatch.setenv("COLONY_ALLOW_OPEN_BIND", "1")
    _guard_bind_auth("0.0.0.0")  # override: must not exit
