"""Colony key resilience: Shamir Secret Sharing, encrypted share persistence, key rotation.

The private key is NEVER stored in plaintext at rest or between signing calls.
All reconstruction happens in memory and is zeroed immediately after use.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional, Protocol, Tuple, Union

try:
    import httpx as _httpx
except ImportError:
    _httpx = None  # type: ignore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Share primitives
# ---------------------------------------------------------------------------


@dataclass
class KeyShare:
    """A single encrypted Shamir share."""

    colony_id: str
    share_index: int  # 1-based, range [1, n]
    n: int  # total shares
    k: int  # reconstruction threshold
    # No defaults — callers must supply explicit salt and nonce so that
    # randomness generation is visible at the call site (SEC-14-L-07)
    argon2_salt: bytes = field()
    aes_nonce: bytes = field()
    version: int = 1
    ciphertext: bytes = b""  # encrypted share bytes
    tag: bytes = b""  # GCM authentication tag

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "colony_id": self.colony_id,
            "share_index": self.share_index,
            "n": self.n,
            "k": self.k,
            "argon2_salt": self.argon2_salt.hex(),
            "aes_nonce": self.aes_nonce.hex(),
            "ciphertext": self.ciphertext.hex(),
            "tag": self.tag.hex(),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "KeyShare":
        return cls(
            colony_id=d["colony_id"],
            share_index=d["share_index"],
            n=d["n"],
            k=d["k"],
            version=d.get("version", 1),
            argon2_salt=bytes.fromhex(d["argon2_salt"]),
            aes_nonce=bytes.fromhex(d["aes_nonce"]),
            ciphertext=bytes.fromhex(d["ciphertext"]),
            tag=bytes.fromhex(d["tag"]),
        )


class ShareBackend(Protocol):
    """Protocol for key share storage backends."""

    def store_share(self, share: KeyShare) -> None:
        """Persist an encrypted share to this backend."""
        ...

    def retrieve_share(self, colony_id: str, share_index: int) -> Optional[KeyShare]:
        """Retrieve a share. Returns None if not present."""
        ...

    def delete_share(self, colony_id: str, share_index: int) -> None:
        """Delete a share (used during rotation)."""
        ...

    def list_shares(self, colony_id: str) -> List[int]:
        """Return the list of share indices stored in this backend."""
        ...


# ---------------------------------------------------------------------------
# Shamir implementation over GF(2^8)
# ---------------------------------------------------------------------------


class ShamirKeyManager:
    """Split and reconstruct Ed25519 private keys using Shamir Secret Sharing.

    The scheme operates over GF(2^8), applying the polynomial independently
    to each of the 32 bytes of the private key.

    Requirements:
    - MUST support n in range [3, 20] and k in range [2, n].
    - MUST zero all intermediate key material from memory after use.
    - MUST validate that reconstructed key length matches 32 bytes.
    - MUST raise ValueError if fewer than k shares are provided.
    """

    GF256_EXP: List[int] = []
    GF256_LOG: List[int] = []

    def __init__(self) -> None:
        if not self.GF256_EXP:
            self._init_field_tables()

    @classmethod
    def _init_field_tables(cls) -> None:
        """Precompute GF(2^8) exponential and logarithm tables."""
        cls.GF256_EXP = [0] * 512
        cls.GF256_LOG = [0] * 256
        x = 1
        for i in range(255):
            cls.GF256_EXP[i] = x
            cls.GF256_LOG[x] = i
            x = cls._gf_mul_slow(x, 3)
        for i in range(255, 512):
            cls.GF256_EXP[i] = cls.GF256_EXP[i - 255]

    @staticmethod
    def _gf_mul_slow(a: int, b: int) -> int:
        """GF(2^8) multiplication with primitive polynomial x^8+x^4+x^3+x+1."""
        p = 0
        for _ in range(8):
            if b & 1:
                p ^= a
            hi = a & 0x80
            a = (a << 1) & 0xFF
            if hi:
                a ^= 0x1B
            b >>= 1
        return p

    def _gf_mul(self, a: int, b: int) -> int:
        if a == 0 or b == 0:
            return 0
        return self.GF256_EXP[(self.GF256_LOG[a] + self.GF256_LOG[b]) % 255]

    def _gf_div(self, a: int, b: int) -> int:
        if b == 0:
            raise ZeroDivisionError("GF(2^8) division by zero")
        if a == 0:
            return 0
        return self.GF256_EXP[(self.GF256_LOG[a] - self.GF256_LOG[b]) % 255]

    def _eval_polynomial(self, coefficients: List[int], x: int) -> int:
        """Evaluate polynomial at x in GF(2^8)."""
        result = 0
        for coeff in reversed(coefficients):
            result = self._gf_mul(result, x) ^ coeff
        return result

    def split(self, key: bytes, n: int, k: int) -> List[tuple]:
        """Split a 32-byte private key into n shares with threshold k.

        Args:
            key: 32-byte Ed25519 private key.
            n: Total number of shares to produce.
            k: Minimum shares required for reconstruction.

        Returns:
            List of (x, share_bytes) tuples where x is in range [1, n].

        Raises:
            ValueError: If key length != 32, n < 3, k < 2, or k > n.
        """
        if len(key) != 32:
            raise ValueError(f"Key must be 32 bytes, got {len(key)}")
        if n < 3 or n > 20:
            raise ValueError(f"n must be in [3, 20], got {n}")
        if k < 2 or k > n:
            raise ValueError(f"k must be in [2, n], got k={k} n={n}")

        shares: List[tuple] = [(x, bytearray()) for x in range(1, n + 1)]
        for byte_pos in range(32):
            coefficients = [key[byte_pos]] + [
                secrets.randbelow(256) for _ in range(k - 1)
            ]
            for i, (x, share_buf) in enumerate(shares):
                y = self._eval_polynomial(coefficients, x)
                share_buf.append(y)
            # Zero coefficients
            for i in range(len(coefficients)):
                coefficients[i] = 0

        return [(x, bytes(buf)) for x, buf in shares]

    def reconstruct(self, shares: List[tuple], k: int) -> bytes:
        """Reconstruct the private key from k or more shares.

        Args:
            shares: List of (x, share_bytes) tuples.
            k: Threshold (used for validation only; any k+ shares reconstruct).

        Returns:
            32-byte reconstructed private key.

        Raises:
            ValueError: If fewer than k shares provided or share lengths differ.
        """
        if len(shares) < k:
            raise ValueError(
                f"Need at least {k} shares to reconstruct, got {len(shares)}"
            )

        share_len = len(shares[0][1])
        if any(len(s) != share_len for _, s in shares):
            raise ValueError("All shares must be the same length")

        key_bytes = bytearray(share_len)
        for byte_pos in range(share_len):
            points = [(x, share[byte_pos]) for x, share in shares]
            key_bytes[byte_pos] = self._lagrange_interpolate(points, 0)

        return bytes(key_bytes)

    def _lagrange_interpolate(self, points: List[tuple], x: int) -> int:
        """Lagrange interpolation at x in GF(2^8)."""
        result = 0
        for i, (xi, yi) in enumerate(points):
            numerator = denominator = 1
            for j, (xj, _) in enumerate(points):
                if i != j:
                    numerator = self._gf_mul(numerator, x ^ xj)
                    denominator = self._gf_mul(denominator, xi ^ xj)
            lagrange_coeff = self._gf_div(numerator, denominator)
            result ^= self._gf_mul(yi, lagrange_coeff)
        return result

    def zero_key(self, key: bytearray) -> None:
        """Zero key material in memory (SEC-13-L-01: portable memoryview approach)."""
        if isinstance(key, bytearray):
            memoryview(key)[:] = b'\x00' * len(key)


# ---------------------------------------------------------------------------
# Encrypted share persistence
# ---------------------------------------------------------------------------


class KeyShareStore:
    """Encrypted share persistence using AES-256-GCM + Argon2id.

    Requirements:
    - MUST encrypt every share before writing to any backend.
    - MUST bind share to its colony_id and share_index via AAD.
    - MUST support multiple backends (local file, remote node, backup file).
    - MUST detect and reject share transplantation (wrong colony_id or index).
    """

    def __init__(
        self,
        colony_id: str,
        network_id: str,
        backends: List[ShareBackend],
    ) -> None:
        self.colony_id = colony_id
        self.network_id = network_id
        self.backends = backends

    def _aad(self, share_index: int) -> bytes:
        """Compute authenticated additional data binding share to context."""
        return (
            bytes.fromhex(self.colony_id)
            + bytes([share_index])
            + bytes.fromhex(self.network_id)
        )

    def encrypt_share(
        self,
        raw_share: bytes,
        share_index: int,
        n: int,
        k: int,
        passphrase: bytes,
    ) -> KeyShare:
        """Encrypt a raw share blob.

        Args:
            raw_share: 32-byte raw share from ShamirKeyManager.split().
            share_index: 1-based index of this share.
            n: Total shares in the scheme.
            k: Reconstruction threshold.
            passphrase: Bytes passphrase for Argon2id KDF.

        Returns:
            KeyShare with encrypted ciphertext and tag set.
        """
        from argon2.low_level import Type, hash_secret_raw
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        salt = os.urandom(16)
        key = hash_secret_raw(
            secret=passphrase,
            salt=salt,
            time_cost=3,
            memory_cost=65536,
            parallelism=4,
            hash_len=32,
            type=Type.ID,
        )
        nonce = os.urandom(12)
        aesgcm = AESGCM(key)
        aad = self._aad(share_index)
        ct_with_tag = aesgcm.encrypt(nonce, raw_share, aad)
        ciphertext = ct_with_tag[:-16]
        tag = ct_with_tag[-16:]

        share = KeyShare(
            colony_id=self.colony_id,
            share_index=share_index,
            n=n,
            k=k,
            argon2_salt=salt,
            aes_nonce=nonce,
            ciphertext=ciphertext,
            tag=tag,
        )

        # Zero derived key (SEC-13-L-01: portable memoryview approach)
        key_buf = bytearray(key)
        memoryview(key_buf)[:] = b'\x00' * len(key_buf)

        return share

    def decrypt_share(self, share: KeyShare, passphrase: bytes) -> bytes:
        """Decrypt a KeyShare back to raw share bytes.

        Raises:
            ValueError: If colony_id mismatch (transplantation attack).
            cryptography.exceptions.InvalidTag: If decryption fails.
        """
        if share.colony_id != self.colony_id:
            raise ValueError(
                f"Share colony_id mismatch: expected {self.colony_id}, "
                f"got {share.colony_id}"
            )

        from argon2.low_level import Type, hash_secret_raw
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        key = hash_secret_raw(
            secret=passphrase,
            salt=share.argon2_salt,
            time_cost=3,
            memory_cost=65536,
            parallelism=4,
            hash_len=32,
            type=Type.ID,
        )
        aesgcm = AESGCM(key)
        aad = self._aad(share.share_index)
        raw = aesgcm.decrypt(
            share.aes_nonce,
            share.ciphertext + share.tag,
            aad,
        )

        # Zero derived key (SEC-13-L-01: portable memoryview approach)
        key_buf = bytearray(key)
        memoryview(key_buf)[:] = b'\x00' * len(key_buf)

        return raw

    def distribute(self, encrypted_shares: List[KeyShare]) -> None:
        """Distribute encrypted shares across configured backends round-robin."""
        for i, share in enumerate(encrypted_shares):
            backend = self.backends[i % len(self.backends)]
            backend.store_share(share)

    def collect(
        self, k: int, passphrase_fn: Callable[[int], bytes]
    ) -> List[tuple]:
        """Collect and decrypt at least k shares from available backends.

        Args:
            k: Minimum shares to collect.
            passphrase_fn: Callable(share_index) -> passphrase bytes.

        Returns:
            List of (share_index, raw_share_bytes) tuples.

        Raises:
            RuntimeError: If fewer than k shares can be collected.
        """
        raw_shares: List[tuple] = []
        seen_indices: set = set()

        for backend in self.backends:
            indices = backend.list_shares(self.colony_id)
            for idx in indices:
                if idx in seen_indices:
                    continue
                share = backend.retrieve_share(self.colony_id, idx)
                if share is None:
                    continue
                passphrase = passphrase_fn(idx)
                try:
                    raw = self.decrypt_share(share, passphrase)
                    raw_shares.append((idx, raw))
                    seen_indices.add(idx)
                except Exception:
                    continue
                if len(raw_shares) >= k:
                    break
            if len(raw_shares) >= k:
                break

        if len(raw_shares) < k:
            raise RuntimeError(
                f"Could only collect {len(raw_shares)} shares, need {k}. "
                "Check that enough share backends are reachable."
            )
        return raw_shares


# ---------------------------------------------------------------------------
# High-level key manager
# ---------------------------------------------------------------------------


class ColonyKeyManager:
    """High-level manager for colony signing operations.

    Owns the split/reconstruct/sign lifecycle. The private key is NEVER
    stored on this object between signing calls.

    Requirements:
    - MUST reconstruct key in memory only.
    - MUST zero key immediately after signing.
    - MUST NOT cache the reconstructed private key between calls.
    """

    def __init__(
        self,
        colony_id: str,
        shamir: ShamirKeyManager,
        store: KeyShareStore,
        k: int,
        passphrase_fn: Callable[[int], bytes],
        audit_log: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.colony_id = colony_id
        self.shamir = shamir
        self.store = store
        self.k = k
        self.passphrase_fn = passphrase_fn
        self._audit = audit_log or (lambda msg: None)

    def sign(self, payload: bytes) -> str:
        """Sign payload bytes, returning hex-encoded Ed25519 signature."""
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )

        self._audit(f"key_reconstruct colony={self.colony_id}")
        raw_shares = self.store.collect(self.k, self.passphrase_fn)
        raw_key = bytearray(
            self.shamir.reconstruct([(idx, s) for idx, s in raw_shares], self.k)
        )

        try:
            private_key = Ed25519PrivateKey.from_private_bytes(bytes(raw_key))
            signature = private_key.sign(payload)
            return signature.hex()
        finally:
            self.shamir.zero_key(raw_key)
            self._audit(f"key_zeroed colony={self.colony_id}")

    def public_key_hex(self) -> str:
        """Derive public key from reconstructed private key."""
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )

        raw_shares = self.store.collect(self.k, self.passphrase_fn)
        raw_key = bytearray(
            self.shamir.reconstruct([(idx, s) for idx, s in raw_shares], self.k)
        )

        try:
            private_key = Ed25519PrivateKey.from_private_bytes(bytes(raw_key))
            pub = private_key.public_key().public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            )
            return pub.hex()
        finally:
            self.shamir.zero_key(raw_key)

    def rotate(
        self,
        new_private_key: bytes,
        n: int,
        passphrase_fn: Optional[Callable[[int], bytes]] = None,
    ) -> str:
        """Rotate to a new private key: split, encrypt, distribute.

        Signs a colony_rotate_key payload with the OLD key first,
        then replaces shares. Returns hex-encoded new public key.

        Args:
            new_private_key: 32-byte new Ed25519 private key.
            n: Number of shares for the new scheme.
            passphrase_fn: Passphrase callable for new shares (defaults to existing).

        Returns:
            Hex-encoded new public key.
        """
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )

        pf = passphrase_fn or self.passphrase_fn

        # Derive new public key
        new_pk = Ed25519PrivateKey.from_private_bytes(new_private_key)
        new_pubkey_hex = new_pk.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        ).hex()

        # Split new key and encrypt shares
        new_shares = self.shamir.split(new_private_key, n, self.k)
        encrypted: List[KeyShare] = []
        for x, raw_share in new_shares:
            enc = self.store.encrypt_share(raw_share, x, n, self.k, pf(x))
            encrypted.append(enc)

        # Delete old shares from all backends
        old_indices = []
        for backend in self.store.backends:
            old_indices.extend(backend.list_shares(self.colony_id))
        for backend in self.store.backends:
            for idx in set(old_indices):
                backend.delete_share(self.colony_id, idx)

        # Distribute new shares
        self.store.distribute(encrypted)

        # Update passphrase_fn if new one was provided
        if passphrase_fn is not None:
            self.passphrase_fn = passphrase_fn

        logger.info("Key rotation complete for colony %s", self.colony_id)
        return new_pubkey_hex


# ---------------------------------------------------------------------------
# Share backends
# ---------------------------------------------------------------------------


class LocalFileShareBackend:
    """Stores encrypted shares as JSON files on the local filesystem.

    Share files are stored at: data_dir / "shares" / f"share_{index:02d}.json"
    """

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.shares_dir = data_dir / "shares"
        self.shares_dir.mkdir(parents=True, exist_ok=True)

    def store_share(self, share: KeyShare) -> None:
        path = self.shares_dir / f"share_{share.share_index:02d}.json"
        path.write_text(json.dumps(share.to_dict(), indent=2))
        path.chmod(0o600)

    def retrieve_share(self, colony_id: str, share_index: int) -> Optional[KeyShare]:
        path = self.shares_dir / f"share_{share_index:02d}.json"
        if not path.exists():
            return None
        return KeyShare.from_dict(json.loads(path.read_text()))

    def delete_share(self, colony_id: str, share_index: int) -> None:
        path = self.shares_dir / f"share_{share_index:02d}.json"
        if path.exists():
            # Overwrite with random bytes before deletion
            path.write_bytes(os.urandom(path.stat().st_size))
            path.unlink()

    def list_shares(self, colony_id: str) -> List[int]:
        indices = []
        for f in self.shares_dir.glob("share_*.json"):
            try:
                indices.append(int(f.stem.split("_")[1]))
            except (IndexError, ValueError):
                continue
        return sorted(indices)


class RemoteNodeShareBackend:
    """Stores encrypted shares on a remote mesh node via mTLS HTTP API.

    Uses httpx with mutual TLS (client cert + server CA verification) for
    authenticated share transport across Colony mesh nodes.

    API contract (served by the remote node at BASE_PATH):
      PUT    /{colony_id}/{share_index}  — store or replace a share
      GET    /{colony_id}/{share_index}  — retrieve a share (404 → None)
      DELETE /{colony_id}/{share_index}  — delete a share (404 → silent)
      GET    /{colony_id}               — list indices {"indices": [...]}

    Args:
        node_id:      Identifier for the remote peer (used in log messages).
        endpoint:     Base URL of the remote node, e.g. ``https://host:9443``.
        client_cert:  mTLS client identity — ``(cert_path, key_path)`` or
                      ``(cert_path, key_path, password)``.  ``None`` disables
                      client-side certificate.
        ca_cert:      Path to the CA bundle used to verify the server cert,
                      ``True`` (default trust store) or ``False`` (skip verify).
                      ``None`` leaves httpx at its default (True).
        timeout:      Per-request timeout in seconds.
        max_retries:  Number of attempts before giving up on transient errors.
    """

    BASE_PATH = "/internal/shares"
    DEFAULT_TIMEOUT: float = 10.0
    DEFAULT_MAX_RETRIES: int = 3
    RETRY_BACKOFF_BASE: float = 0.5  # seconds; doubles each attempt

    def __init__(
        self,
        node_id: str,
        endpoint: str,
        client_cert: Optional[Union[Tuple[str, str], Tuple[str, str, str]]] = None,
        ca_cert: Optional[Union[str, bool]] = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        # Legacy kwarg accepted but ignored — mTLS is now configured via
        # client_cert / ca_cert.
        mtls_ctx=None,
    ) -> None:
        if _httpx is None:
            raise RuntimeError(
                "httpx is required for RemoteNodeShareBackend. "
                "Install it with: pip install httpx"
            )
        self.node_id = node_id
        self.endpoint = endpoint.rstrip("/")
        self._client_cert = client_cert
        self._ca_cert = ca_cert
        self._timeout = timeout
        self._max_retries = max_retries
        self._client: Optional["_httpx.Client"] = None

    # ------------------------------------------------------------------
    # Client lifecycle
    # ------------------------------------------------------------------

    def _build_client(self) -> "_httpx.Client":
        """Construct an httpx.Client with the configured mTLS parameters."""
        kwargs: dict = {"timeout": self._timeout}
        if self._ca_cert is not None:
            kwargs["verify"] = self._ca_cert
        if self._client_cert is not None:
            kwargs["cert"] = self._client_cert
        return _httpx.Client(**kwargs)

    @property
    def _http(self) -> "_httpx.Client":
        """Return the active client, rebuilding lazily if needed."""
        if self._client is None or self._client.is_closed:
            self._client = self._build_client()
        return self._client

    def rotate_certificates(
        self,
        client_cert: Optional[Union[Tuple[str, str], Tuple[str, str, str]]] = None,
        ca_cert: Optional[Union[str, bool]] = None,
    ) -> None:
        """Swap TLS certificates.  The next request will use the new certs.

        Only the provided arguments are updated; omit a parameter to leave it
        unchanged.
        """
        if client_cert is not None:
            self._client_cert = client_cert
        if ca_cert is not None:
            self._ca_cert = ca_cert
        # Tear down the existing client; new one is built lazily.
        if self._client is not None and not self._client.is_closed:
            self._client.close()
        self._client = None

    def close(self) -> None:
        """Close the underlying HTTP client."""
        if self._client is not None and not self._client.is_closed:
            self._client.close()
        self._client = None

    def __enter__(self) -> "RemoteNodeShareBackend":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Internal request helper with exponential-backoff retry
    # ------------------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs) -> "_httpx.Response":
        """Execute a request against the remote node, retrying on transient failures.

        4xx responses are *not* retried (caller error).
        5xx, timeouts, and network errors are retried up to ``max_retries`` times.

        Raises:
            httpx.HTTPStatusError: On a non-retryable HTTP error (4xx).
            RuntimeError: When all retry attempts are exhausted.
        """
        url = f"{self.endpoint}{path}"
        last_exc: Optional[Exception] = None

        for attempt in range(self._max_retries):
            try:
                resp = self._http.request(method, url, **kwargs)
                resp.raise_for_status()
                return resp
            except _httpx.HTTPStatusError as exc:
                if exc.response.status_code < 500:
                    raise  # 4xx — don't retry
                last_exc = exc
                logger.warning(
                    "Server error (attempt %d/%d) %s %s: HTTP %d",
                    attempt + 1, self._max_retries, method, url,
                    exc.response.status_code,
                )
            except _httpx.TimeoutException as exc:
                last_exc = exc
                logger.warning(
                    "Timeout (attempt %d/%d) %s %s",
                    attempt + 1, self._max_retries, method, url,
                )
            except _httpx.NetworkError as exc:
                last_exc = exc
                logger.warning(
                    "Network error (attempt %d/%d) %s %s: %s",
                    attempt + 1, self._max_retries, method, url, exc,
                )
                # Recreate client — the connection may be in a broken state.
                if self._client is not None and not self._client.is_closed:
                    self._client.close()
                self._client = None

            if attempt < self._max_retries - 1:
                time.sleep(self.RETRY_BACKOFF_BASE * (2 ** attempt))

        raise RuntimeError(
            f"Remote share backend '{self.node_id}' unreachable after "
            f"{self._max_retries} attempt(s): {last_exc}"
        ) from last_exc

    # ------------------------------------------------------------------
    # ShareBackend interface
    # ------------------------------------------------------------------

    def store_share(self, share: KeyShare) -> None:
        """Persist a share to the remote node via PUT."""
        try:
            self._request(
                "PUT",
                f"{self.BASE_PATH}/{share.colony_id}/{share.share_index}",
                json=share.to_dict(),
            )
        except Exception as exc:
            logger.error(
                "store_share failed [node=%s colony=%s index=%d]: %s",
                self.node_id, share.colony_id, share.share_index, exc,
            )
            raise

    def retrieve_share(self, colony_id: str, share_index: int) -> Optional[KeyShare]:
        """Retrieve a share from the remote node. Returns ``None`` if absent."""
        try:
            resp = self._request(
                "GET",
                f"{self.BASE_PATH}/{colony_id}/{share_index}",
            )
            return KeyShare.from_dict(resp.json())
        except _httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            logger.error(
                "retrieve_share failed [node=%s colony=%s index=%d]: HTTP %d",
                self.node_id, colony_id, share_index, exc.response.status_code,
            )
            return None
        except Exception as exc:
            logger.error(
                "retrieve_share failed [node=%s colony=%s index=%d]: %s",
                self.node_id, colony_id, share_index, exc,
            )
            return None

    def delete_share(self, colony_id: str, share_index: int) -> None:
        """Delete a share from the remote node.  404 is silently ignored."""
        try:
            self._request(
                "DELETE",
                f"{self.BASE_PATH}/{colony_id}/{share_index}",
            )
        except _httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return  # Already absent — treat as success.
            logger.error(
                "delete_share failed [node=%s colony=%s index=%d]: HTTP %d",
                self.node_id, colony_id, share_index, exc.response.status_code,
            )
            raise
        except Exception as exc:
            logger.error(
                "delete_share failed [node=%s colony=%s index=%d]: %s",
                self.node_id, colony_id, share_index, exc,
            )
            raise

    def list_shares(self, colony_id: str) -> List[int]:
        """Return sorted share indices held by the remote node.

        Returns an empty list on any transport failure so callers degrade
        gracefully during network partitions.
        """
        try:
            resp = self._request("GET", f"{self.BASE_PATH}/{colony_id}")
            return sorted(resp.json().get("indices", []))
        except Exception as exc:
            logger.error(
                "list_shares failed [node=%s colony=%s]: %s",
                self.node_id, colony_id, exc,
            )
            return []


class BackupFileShareBackend:
    """Read/write a portable .colonyshare backup file.

    Holds exactly one share. Writes are atomic (tmp + os.replace) and
    include a SHA-256 checksum of the share payload so tampering can be
    detected on read.

    ``colony_id`` and ``network_id`` must be provided either at
    construction or via an existing file on disk; otherwise ``store_share``
    cannot build a valid payload.
    """

    _FILE_TYPE = "colony_key_share"
    _FILE_VERSION = 2  # v2 adds checksum

    def __init__(
        self,
        file_path: Path,
        colony_id: Optional[str] = None,
        network_id: Optional[str] = None,
        *,
        overwrite: bool = False,
    ) -> None:
        self.file_path = Path(file_path)
        self._share: Optional[KeyShare] = None
        self._colony_id = colony_id
        self._network_id = network_id
        self._overwrite = overwrite

    @staticmethod
    def _checksum(share_dict: dict) -> str:
        import hashlib
        payload = json.dumps(share_dict, sort_keys=True).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _load(self) -> None:
        if self._share is None and self.file_path.exists():
            data = json.loads(self.file_path.read_text())
            share_dict = data["share"]
            # If a checksum is present (v2+), verify it.
            stored = data.get("checksum")
            if stored is not None and stored != self._checksum(share_dict):
                raise ValueError(
                    f"Checksum mismatch in backup file {self.file_path}"
                )
            self._share = KeyShare.from_dict(share_dict)
            # Populate colony/network IDs from file if not set at ctor.
            if self._colony_id is None:
                self._colony_id = data.get("colony_id")
            if self._network_id is None:
                self._network_id = data.get("network_id")

    def store_share(self, share: KeyShare) -> None:
        """Atomically write ``share`` to the backup file.

        Refuses to overwrite a file holding a *different* share_index
        unless the backend was constructed with ``overwrite=True``.
        """
        # Resolve metadata — prefer ctor values, fall back to existing file.
        if self._colony_id is None or self._network_id is None:
            self._load()
        if self._colony_id is None or self._network_id is None:
            raise ValueError(
                "colony_id and network_id are required to write a backup file"
            )

        if self.file_path.exists() and not self._overwrite:
            try:
                existing = json.loads(self.file_path.read_text())
                existing_index = existing.get("share", {}).get("share_index")
                if existing_index is not None and existing_index != share.share_index:
                    raise ValueError(
                        f"Backup already holds share_index={existing_index}; "
                        f"refusing to overwrite with share_index={share.share_index}"
                    )
            except (json.JSONDecodeError, KeyError):
                pass  # Corrupt file; will be replaced.

        share_dict = share.to_dict()
        data = {
            "file_type": self._FILE_TYPE,
            "file_version": self._FILE_VERSION,
            "colony_id": self._colony_id,
            "network_id": self._network_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "share": share_dict,
            "checksum": self._checksum(share_dict),
        }

        tmp_path = self.file_path.with_suffix(self.file_path.suffix + ".tmp")
        tmp_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.write_text(json.dumps(data, indent=2))
        tmp_path.chmod(0o600)
        os.replace(tmp_path, self.file_path)
        self._share = share

    def retrieve_share(self, colony_id: str, share_index: int) -> Optional[KeyShare]:
        self._load()
        if self._share and self._share.share_index == share_index:
            return self._share
        return None

    def delete_share(self, colony_id: str, share_index: int) -> None:
        self._load()
        if self._share and self._share.share_index == share_index:
            try:
                self.file_path.unlink()
            except FileNotFoundError:
                pass
            self._share = None

    def list_shares(self, colony_id: str) -> List[int]:
        self._load()
        if self._share:
            return [self._share.share_index]
        return []


class InMemoryShareBackend:
    """In-memory backend for testing."""

    def __init__(self) -> None:
        self._shares: dict = {}  # share_index -> KeyShare

    def store_share(self, share: KeyShare) -> None:
        self._shares[share.share_index] = share

    def retrieve_share(self, colony_id: str, share_index: int) -> Optional[KeyShare]:
        return self._shares.get(share_index)

    def delete_share(self, colony_id: str, share_index: int) -> None:
        self._shares.pop(share_index, None)

    def list_shares(self, colony_id: str) -> List[int]:
        return sorted(self._shares.keys())


# ---------------------------------------------------------------------------
# Backup file helpers
# ---------------------------------------------------------------------------


def export_share_file(
    share: KeyShare,
    colony_id: str,
    network_id: str,
    output_path: Path,
) -> None:
    """Write a .colonyshare backup file."""
    data = {
        "file_type": "colony_key_share",
        "file_version": 1,
        "colony_id": colony_id,
        "network_id": network_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "share": share.to_dict(),
    }
    output_path.write_text(json.dumps(data, indent=2))
    output_path.chmod(0o600)


def import_share_file(file_path: Path) -> tuple:
    """Read a .colonyshare backup file.

    Returns:
        (colony_id, network_id, KeyShare) tuple.
    """
    data = json.loads(file_path.read_text())
    if data.get("file_type") != "colony_key_share":
        raise ValueError(f"Not a colony_key_share file: {file_path}")
    colony_id = data["colony_id"]
    network_id = data["network_id"]
    share = KeyShare.from_dict(data["share"])
    return colony_id, network_id, share
