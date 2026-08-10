"""ProactiveDeliveryBridge — queues and manages proactive message delivery.

The autonomy loop calls deliver() with an initiative or insight. The bridge:
1. Rate-limits per person
2. Queues the message in pending deliveries
3. The gateway polls GET /v1/delivery/pending and sends via platform adapters

Delivery channels:
  PUSH       → deliver immediately (queued for gateway polling)
  IN_SESSION → store for injection into next conversation's system prompt
  DIGEST     → accumulate for bundled morning briefing (wired but not scheduled by default)
"""

from __future__ import annotations

import logging
import os
import uuid
import json
import hashlib
import sqlite3
import stat
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from colony_sidecar.delivery.rate_limiter import DeliveryRateLimiter
from colony_sidecar.delivery.channels import ChannelRegistry
from colony_sidecar.workers.queue_worker import encode_hermes_webhook

logger = logging.getLogger(__name__)

# Default internal port for the gateway's /internal/deliver endpoint.
_DEFAULT_GATEWAY_INTERNAL_PORT = 7779
_GATEWAY_CONTRACTS = frozenset(("legacy_delivery", "governed_admission_v1"))
_GOVERNED_GATEWAY_PLATFORMS = frozenset(("whatsapp",))


def _strict_gateway_json(raw: str) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > 64 * 1024:
        return None

    def pairs(values):
        result: Dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise ValueError("duplicate gateway response field")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw,
            object_pairs_hook=pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError("non-finite gateway response number")
            ),
        )
    except (TypeError, ValueError, UnicodeError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


@dataclass(frozen=True)
class GatewayPushResult:
    """Truthful result of handing one message to a gateway boundary.

    ``accepted`` means only that the immediate boundary accepted the request.
    ``provider_delivered`` is a separate fact so a governed approval queue can
    never be reported as a completed send.
    """

    accepted: bool
    provider_delivered: bool
    contract: str
    admission_state: str = ""
    delivery_id: str = ""
    terminal: bool = False
    observation_new: bool = True

    def __bool__(self) -> bool:
        return self.accepted


class _GatewayOutcomeStore:
    """PII-free durable lifecycle cache for one governed gateway boundary."""

    _STATES = frozenset(
        ("queued", "awaiting_approval", "accepted", "delivered", "failed", "ambiguous")
    )

    def __init__(self, path: Path, *, clock, poll_seconds: float) -> None:
        raw_path = Path(path).expanduser()
        self.path = raw_path if raw_path.is_absolute() else Path.cwd() / raw_path
        self.clock = clock
        self.poll_seconds = float(poll_seconds)
        self._lock = threading.RLock()
        # resolve() would hide a symlink before the safety check.  Inspect the
        # existing ancestry before and after creating a missing private state
        # directory, and let SQLite see the same unresolved absolute path.
        for component in (self.path, *self.path.parents):
            if component.is_symlink():
                raise ValueError("gateway outcome path is unsafe")
        parent_existed = self.path.parent.exists()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not parent_existed:
            os.chmod(self.path.parent, 0o700)
        for component in (self.path, *self.path.parents):
            if component.is_symlink():
                raise ValueError("gateway outcome path is unsafe")
        self._conn = sqlite3.connect(
            str(self.path), timeout=30.0, isolation_level=None, check_same_thread=False
        )
        os.chmod(self.path, 0o600)
        if stat.S_IMODE(self.path.stat().st_mode) != 0o600:
            self._conn.close()
            raise ValueError("gateway outcome database must be private")
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.execute("PRAGMA journal_mode=DELETE")
        self._conn.execute("PRAGMA synchronous=FULL")
        version = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
        if version not in (0, 1):
            self._conn.close()
            raise ValueError("gateway outcome schema is unsupported")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS governed_gateway_outcomes (
                delivery_id TEXT PRIMARY KEY,
                request_sha256 TEXT NOT NULL,
                state TEXT NOT NULL,
                intent_id TEXT NOT NULL,
                provider_delivered INTEGER NOT NULL,
                next_poll_at REAL NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            """
        )
        if version == 0:
            self._conn.execute("PRAGMA user_version=1")

    @staticmethod
    def _row(row) -> Dict[str, Any]:
        value = dict(row)
        if value.get("state") not in _GatewayOutcomeStore._STATES:
            raise ValueError("gateway outcome state is invalid")
        value["provider_delivered"] = bool(value["provider_delivered"])
        return value

    def reserve(self, delivery_id: str, request_sha256: str) -> Dict[str, Any]:
        now = float(self.clock())
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT * FROM governed_gateway_outcomes WHERE delivery_id=?",
                    (delivery_id,),
                ).fetchone()
                if row is None:
                    self._conn.execute(
                        """INSERT INTO governed_gateway_outcomes
                           (delivery_id,request_sha256,state,intent_id,
                            provider_delivered,next_poll_at,created_at,updated_at)
                           VALUES (?,?,'queued','',0,0,?,?)""",
                        (delivery_id, request_sha256, now, now),
                    )
                elif row["request_sha256"] != request_sha256:
                    raise ValueError("gateway delivery identity conflicts")
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return self.get(delivery_id)

    def get(self, delivery_id: str) -> Dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM governed_gateway_outcomes WHERE delivery_id=?",
                (delivery_id,),
            ).fetchone()
        if row is None:
            raise ValueError("gateway outcome is unavailable")
        return self._row(row)

    def observe(
        self, delivery_id: str, *, state: str, intent_id: str,
        provider_delivered: bool,
    ) -> tuple[Dict[str, Any], bool]:
        if state not in self._STATES or state == "queued":
            raise ValueError("gateway outcome transition is invalid")
        if provider_delivered is not (state == "delivered"):
            raise ValueError("gateway provider-delivery outcome is inconsistent")
        now = float(self.clock())
        terminal = state in {"delivered", "failed", "ambiguous"}
        next_poll_at = 0.0 if terminal else now + self.poll_seconds
        with self._lock:
            current = self.get(delivery_id)
            transitions = {
                "queued": {
                    "awaiting_approval", "accepted", "delivered", "failed", "ambiguous",
                },
                "awaiting_approval": {
                    "awaiting_approval", "accepted", "delivered", "failed", "ambiguous",
                },
                "accepted": {"accepted", "delivered", "failed", "ambiguous"},
                "delivered": {"delivered"},
                "failed": {"failed"},
                "ambiguous": {"ambiguous"},
            }
            if state not in transitions[current["state"]]:
                raise ValueError("gateway outcome transition regressed")
            # A governed intent is immutable once the producer has admitted
            # it. A direct provider delivery may legitimately be the first
            # and only observation and carry the boundary's empty intent ID.
            # Every pending or failed/ambiguous producer observation requires
            # a nonempty intent, and every later observation must match it.
            current_intent = str(current["intent_id"])
            if current["state"] == "queued":
                if state != "delivered" and not intent_id:
                    raise ValueError("gateway intent identity is missing")
            elif not current_intent or intent_id != current_intent:
                raise ValueError("gateway intent identity changed")
            if current["state"] in {"delivered", "failed", "ambiguous"}:
                if (
                    state != current["state"]
                    or intent_id != current_intent
                    or provider_delivered is not current["provider_delivered"]
                ):
                    raise ValueError("gateway terminal outcome is immutable")
                return current, False
            changed = bool(
                current["state"] != state
                or current["intent_id"] != intent_id
                or current["provider_delivered"] is not provider_delivered
            )
            self._conn.execute(
                """UPDATE governed_gateway_outcomes
                   SET state=?,intent_id=?,provider_delivered=?,next_poll_at=?,updated_at=?
                   WHERE delivery_id=?""",
                (
                    state, intent_id, 1 if provider_delivered else 0,
                    next_poll_at, now, delivery_id,
                ),
            )
        return self.get(delivery_id), changed

    def defer(self, delivery_id: str) -> Dict[str, Any]:
        now = float(self.clock())
        with self._lock:
            self._conn.execute(
                """UPDATE governed_gateway_outcomes
                   SET next_poll_at=?,updated_at=? WHERE delivery_id=?""",
                (now + self.poll_seconds, now, delivery_id),
            )
        return self.get(delivery_id)

    def pending_delivery_ids(self, *, limit: int = 100) -> tuple[str, ...]:
        bounded = max(1, min(100, int(limit)))
        with self._lock:
            rows = self._conn.execute(
                """SELECT delivery_id FROM governed_gateway_outcomes
                   WHERE state IN ('accepted','awaiting_approval')
                   ORDER BY created_at ASC,delivery_id ASC LIMIT ?""",
                (bounded,),
            ).fetchall()
        return tuple(str(row["delivery_id"]) for row in rows)


@dataclass
class PendingDelivery:
    """A proactive message waiting to be sent to a user."""
    delivery_id: str
    person_id: str
    content: str
    channel: str          # "push" | "in_session" | "digest"
    urgency: float
    source: str           # "initiative" | "insight" | "anomaly"
    initiative_id: Optional[str]
    queued_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    sent: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)


class ProactiveDeliveryBridge:
    """Routes autonomy loop outputs (initiatives, insights) to users via the gateway.

    Two delivery paths are supported:

    1. **Poll path** (default): The gateway polls /v1/delivery/pending every few
       seconds and POSTs each pending delivery to the appropriate platform adapter.

    2. **Push path** (when gateway_url is set): ``push_to_gateway()`` POSTs
       directly to the gateway's internal ``POST /internal/deliver`` endpoint so
       messages are delivered immediately without polling latency.
    """

    def __init__(
        self,
        rate_limiter: Optional[DeliveryRateLimiter] = None,
        gateway_url: Optional[str] = None,
        gateway_api_key: Optional[str] = None,
        channel_registry: Optional[ChannelRegistry] = None,
        gateway_contract: Optional[str] = None,
        gateway_outcome_db: Optional[str] = None,
        gateway_poll_seconds: float = 5.0,
        clock=time.time,
    ) -> None:
        if rate_limiter is None:
            # Persist rate-limit state so a crashloop can't reset the daily
            # caps. Lives alongside other sidecar state under COLONY_STATE_DIR.
            state_dir = os.environ.get("COLONY_STATE_DIR", ".")
            db_path = Path(state_dir) / "colony-delivery-rate-limit.db"
            rate_limiter = DeliveryRateLimiter(db_path=db_path)
        self._rate_limiter = rate_limiter
        self._pending: List[PendingDelivery] = []
        self._sent: List[PendingDelivery] = []  # short history for observability
        self._sent_max: int = 500  # cap to prevent unbounded growth

        # Gateway push path — optional direct delivery via /internal/deliver
        _port = int(os.environ.get("COLONY_GATEWAY_INTERNAL_PORT", _DEFAULT_GATEWAY_INTERNAL_PORT))
        self._gateway_url: str = (
            gateway_url
            or os.environ.get("COLONY_GATEWAY_INTERNAL_URL", "")
            or f"http://localhost:{_port}"
        )
        self._gateway_api_key: str = (
            gateway_api_key
            or os.environ.get("COLONY_GATEWAY_API_KEY", "")
            or os.environ.get("COLONY_API_KEY", "")
        )
        self._gateway_contract = str(
            gateway_contract
            or os.environ.get("COLONY_GATEWAY_CONTRACT", "legacy_delivery")
        ).strip().lower()
        if self._gateway_contract not in _GATEWAY_CONTRACTS:
            raise ValueError("unsupported gateway response contract")
        if self._gateway_contract == "governed_admission_v1":
            parsed = urllib.parse.urlsplit(self._gateway_url)
            if (
                parsed.scheme != "http"
                or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or parsed.path not in ("", "/")
                or parsed.port is None
                or not self._gateway_api_key
            ):
                raise ValueError(
                    "governed gateway admission requires one authenticated loopback origin"
                )
        try:
            bounded_poll = float(gateway_poll_seconds)
        except (TypeError, ValueError) as error:
            raise ValueError("gateway lifecycle poll interval is invalid") from error
        if not 1.0 <= bounded_poll <= 300.0:
            raise ValueError("gateway lifecycle poll interval is invalid")
        self._clock = clock
        self._gateway_poll_seconds = bounded_poll
        self._gateway_outcomes: Optional[_GatewayOutcomeStore] = None
        if self._gateway_contract == "governed_admission_v1":
            state_dir = Path(os.environ.get("COLONY_STATE_DIR", "."))
            outcome_path = Path(gateway_outcome_db) if gateway_outcome_db else (
                state_dir / "colony-governed-gateway-outcomes.db"
            )
            self._gateway_outcomes = _GatewayOutcomeStore(
                outcome_path, clock=clock, poll_seconds=bounded_poll
            )

        # Channel registry for per-person delivery routing
        self._channel_registry = channel_registry or ChannelRegistry.load()

        # Home channel config read from env vars — used to resolve
        # platform/chat_id when only person_id is available.
        self._home_channels: Dict[str, Dict[str, str]] = self._load_home_channels()

    def governed_gateway_admission_enabled(
        self, platform: Optional[str] = None,
    ) -> bool:
        """Whether governed admission owns this exact platform route.

        A no-argument call reports whether lifecycle reconciliation should run.
        Send-authority callers must provide a platform so a governed WhatsApp
        sidecar cannot silently exempt an RCS/SMS route from Colony's legacy
        non-owner approval gate.
        """

        if self._gateway_contract != "governed_admission_v1":
            return False
        if platform is None:
            return True
        return str(platform).strip().lower() in _GOVERNED_GATEWAY_PLATFORMS

    def _gateway_contract_for_platform(self, platform: str) -> str:
        if self.governed_gateway_admission_enabled(platform):
            return "governed_admission_v1"
        return "legacy_delivery"

    def governed_gateway_poll_seconds(self) -> float:
        """Bounded cadence for unattended governed-lifecycle reconciliation."""

        return self._gateway_poll_seconds

    def governed_pending_delivery_ids(self, *, limit: int = 100) -> tuple[str, ...]:
        """PII-free durable identities that still need a terminal observation."""

        if self._gateway_outcomes is None:
            return ()
        return self._gateway_outcomes.pending_delivery_ids(limit=limit)

    def _stored_gateway_result(
        self, row: Dict[str, Any], *, observation_new: bool = False
    ) -> GatewayPushResult:
        state = str(row["state"])
        return GatewayPushResult(
            accepted=state in {"accepted", "awaiting_approval", "delivered"},
            provider_delivered=row["provider_delivered"] is True,
            contract=self._gateway_contract,
            admission_state=state,
            delivery_id=str(row["delivery_id"]),
            terminal=state in {"delivered", "failed", "ambiguous"},
            observation_new=observation_new,
        )

    def _defer_or_fail_gateway(
        self,
        row: Optional[Dict[str, Any]],
        delivery_id: str,
        *,
        gateway_contract: Optional[str] = None,
    ) -> GatewayPushResult:
        effective_contract = gateway_contract or self._gateway_contract
        if (
            self._gateway_outcomes is not None
            and row is not None
            and row.get("state") in {"accepted", "awaiting_approval"}
        ):
            return self._stored_gateway_result(
                self._gateway_outcomes.defer(delivery_id), observation_new=False
            )
        return GatewayPushResult(
            False, False, effective_contract,
            delivery_id=delivery_id, observation_new=False,
        )

    # ------------------------------------------------------------------
    # Home channel resolution
    # ------------------------------------------------------------------

    @staticmethod
    def _load_home_channels() -> Dict[str, Dict[str, str]]:
        """Build a {platform: {chat_id, name}} mapping from env vars."""
        channels: Dict[str, Dict[str, str]] = {}
        env_map = {
            "telegram": ("TELEGRAM_HOME_CHANNEL", "TELEGRAM_HOME_CHANNEL_NAME"),
            "whatsapp": ("WHATSAPP_HOME_CHANNEL", "WHATSAPP_HOME_CHANNEL_NAME"),
            "discord": ("DISCORD_HOME_CHANNEL", "DISCORD_HOME_CHANNEL_NAME"),
            "slack": ("SLACK_HOME_CHANNEL", "SLACK_HOME_CHANNEL_NAME"),
            "signal": ("SIGNAL_HOME_CHANNEL", "SIGNAL_HOME_CHANNEL_NAME"),
        }
        for platform, (chat_env, name_env) in env_map.items():
            chat_id = os.environ.get(chat_env, "")
            if chat_id:
                channels[platform] = {
                    "chat_id": chat_id,
                    "name": os.environ.get(name_env, platform.title()),
                }
        return channels

    def resolve_home_channel(self) -> Optional[Dict[str, str]]:
        """Resolve the first configured home channel.
        
        Returns:
            Dict with platform, chat_id, account_id or None if not configured.
            Platform is normalized to lowercase OpenClaw channel name.
        """
        for platform, info in self._home_channels.items():
            return {
                "platform": platform.lower(),  # whatsapp, telegram, discord, slack, signal
                "chat_id": info["chat_id"],
                "account_id": "default",  # Could be made configurable later
            }
        return None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def deliver(
        self,
        person_id: str,
        content: str,
        channel: str = "push",
        urgency: float = 0.5,
        source: str = "initiative",
        initiative_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """Queue a proactive message for delivery.

        Returns the delivery_id if queued, None if rate-limited.
        """
        allowed, reason = self._rate_limiter.can_deliver(person_id, urgency=urgency)
        if not allowed:
            logger.debug(
                "Proactive delivery blocked for %s: %s (urgency=%.2f)",
                person_id,
                reason,
                urgency,
            )
            return None

        delivery = PendingDelivery(
            delivery_id=str(uuid.uuid4()),
            person_id=person_id,
            content=content,
            channel=channel,
            urgency=urgency,
            source=source,
            initiative_id=initiative_id,
            metadata=metadata or {},
        )
        self._pending.append(delivery)
        logger.info(
            "Proactive delivery queued: %s → %s (channel=%s, urgency=%.2f)",
            delivery.delivery_id,
            person_id,
            channel,
            urgency,
        )
        return delivery.delivery_id

    async def push_to_gateway(
        self,
        platform: str,
        chat_id: str,
        message: str,
        source: str = "initiative",
        delivery_id: str = "",
        source_id: str = "",
    ) -> GatewayPushResult:
        """Push a proactive message directly to the gateway's /internal/deliver endpoint.

        The legacy contract preserves its historic HTTP-200 delivery meaning.
        The opt-in governed contract durably tracks the exact boundary outcome
        from admission through provider delivery or terminal failure.  The
        caller remains responsible for prior rate-limit checks if needed.
        """
        gateway_contract = self._gateway_contract_for_platform(platform)
        payload = {
            "platform": platform,
            "chat_id": chat_id,
            "message": message,
            "source": source,
        }
        # Optional, deployment-neutral correlation fields.  Existing gateways
        # continue to receive the original four-field contract when callers do
        # not provide them.  Governed sidecars can use these stable source
        # identities to make retries durable and idempotent without teaching
        # Colony anything about a deployment's authority model.
        if delivery_id:
            payload["delivery_id"] = delivery_id
        if source_id:
            payload["source_id"] = source_id

        stored: Optional[Dict[str, Any]] = None
        if gateway_contract == "governed_admission_v1":
            if not delivery_id or not source_id or self._gateway_outcomes is None:
                logger.warning(
                    "Governed gateway lifecycle requires stable delivery and source IDs"
                )
                return GatewayPushResult(
                    False,
                    False,
                    gateway_contract,
                    delivery_id=delivery_id,
                    observation_new=False,
                )
            request_sha256 = hashlib.sha256(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
            try:
                stored = self._gateway_outcomes.reserve(delivery_id, request_sha256)
            except (OSError, sqlite3.Error, ValueError):
                logger.warning(
                    "Governed gateway delivery identity could not be reserved",
                    exc_info=True,
                )
                return GatewayPushResult(
                    False,
                    False,
                    gateway_contract,
                    delivery_id=delivery_id,
                    observation_new=False,
                )
            if stored["state"] in {"delivered", "failed", "ambiguous"}:
                return self._stored_gateway_result(stored, observation_new=False)
            if (
                stored["state"] in {"accepted", "awaiting_approval"}
                and float(stored["next_poll_at"]) > float(self._clock())
            ):
                return self._stored_gateway_result(stored, observation_new=False)

        try:
            import aiohttp
        except ImportError:
            logger.warning("aiohttp not available — cannot push to gateway")
            return self._defer_or_fail_gateway(
                stored, delivery_id, gateway_contract=gateway_contract,
            )

        url = f"{self._gateway_url.rstrip('/')}/internal/deliver"
        headers: Dict[str, str] = {"Content-Type": "application/json"}
        if self._gateway_api_key:
            headers["Authorization"] = f"Bearer {self._gateway_api_key}"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=5.0),
                ) as resp:
                    body = await resp.text()
                    if resp.status == 200 and gateway_contract == "legacy_delivery":
                        logger.info(
                            "Proactive message pushed to gateway (platform=%s, chat_id=%s, source=%s)",
                            platform,
                            chat_id,
                            source,
                        )
                        return GatewayPushResult(
                            True, True, gateway_contract,
                            admission_state="delivered", delivery_id=delivery_id,
                        )
                    if resp.status == 200 and gateway_contract == "governed_admission_v1":
                        content_type = str(
                            getattr(resp, "headers", {}).get("Content-Type", "")
                        ).split(";", 1)[0].strip().lower()
                        document = _strict_gateway_json(body)
                        expected_fields = {
                            "schema", "version", "delivery_id", "state",
                            "intent_id", "provider_delivered",
                        }
                        admission_receipt = bool(
                            isinstance(document, dict)
                            and document.get("provider_delivered") is False
                            and document.get("state") in {
                                "accepted", "awaiting_approval"
                            }
                            and isinstance(document.get("intent_id"), str)
                            and bool(document.get("intent_id"))
                        )
                        delivery_receipt = bool(
                            isinstance(document, dict)
                            and document.get("provider_delivered") is True
                            and document.get("state") == "delivered"
                            and isinstance(document.get("intent_id"), str)
                        )
                        terminal_failure_receipt = bool(
                            isinstance(document, dict)
                            and document.get("provider_delivered") is False
                            and document.get("state") in {"failed", "ambiguous"}
                            and isinstance(document.get("intent_id"), str)
                            and bool(document.get("intent_id"))
                        )
                        if (
                            content_type != "application/json"
                            or not isinstance(document, dict)
                            or set(document) != expected_fields
                            or document.get("schema") != "GatewayBoundaryOutcomeV1"
                            or document.get("version") != 1
                            or document.get("delivery_id") != delivery_id
                            or not (
                                admission_receipt
                                or delivery_receipt
                                or terminal_failure_receipt
                            )
                        ):
                            logger.warning(
                                "Governed gateway admission response failed exact attestation"
                            )
                            return self._defer_or_fail_gateway(
                                stored,
                                delivery_id,
                                gateway_contract=gateway_contract,
                            )
                        if admission_receipt:
                            logger.info(
                                "Gateway admitted proactive message without provider-delivery "
                                "claim (platform=%s, source=%s, state=%s)",
                                platform, source, document["state"],
                            )
                        elif delivery_receipt:
                            logger.info(
                                "Gateway attested provider delivery "
                                "(platform=%s, source=%s)", platform, source,
                            )
                        else:
                            logger.warning(
                                "Gateway attested terminal outcome without delivery "
                                "(platform=%s, source=%s, state=%s)",
                                platform,
                                source,
                                document["state"],
                            )
                        assert self._gateway_outcomes is not None
                        observed, changed = self._gateway_outcomes.observe(
                            delivery_id,
                            state=str(document["state"]),
                            intent_id=str(document["intent_id"]),
                            provider_delivered=bool(document["provider_delivered"]),
                        )
                        return self._stored_gateway_result(
                            observed, observation_new=changed
                        )
                    logger.warning(
                        "Gateway /internal/deliver returned %d: %s",
                        resp.status,
                        body[:200],
                    )
                    return self._defer_or_fail_gateway(
                        stored, delivery_id, gateway_contract=gateway_contract,
                    )
        except Exception as exc:
            logger.warning("push_to_gateway failed: %s", exc)
            return self._defer_or_fail_gateway(
                stored, delivery_id, gateway_contract=gateway_contract,
            )

    def _prepare_initiative_dispatch(self, initiative: Dict[str, Any]) -> Dict[str, Any]:
        """Build everything needed to dispatch an initiative to Hermes.

        Pure/side-effect-free: resolves the recipient bucket and target
        channel, builds the webhook payload, and signs the exact bytes that
        go on the wire. Both :meth:`push_initiative` (which sends) and
        :meth:`preview_initiative` (which does not) share this so the shadow
        view is byte-identical to what a real send would transmit.

        Returns a dict with: url, headers, body_bytes, payload, person_id
        (rate-limit recipient bucket), urgency (0-1), channel_hint, target
        ({user_chat, home_chat}).
        """
        # Hermes webhook URL — override via env var for flexibility
        hermes_webhook_url = os.environ.get(
            "COLONY_HERMES_WEBHOOK_URL",
            "http://127.0.0.1:8644/webhooks/colony-initiatives",
        )

        headers: Dict[str, str] = {"Content-Type": "application/json"}
        webhook_secret = os.environ.get("COLONY_HERMES_WEBHOOK_SECRET", "")

        # Resolve agent name from env var — never hardcode
        agent_name = os.environ.get("COLONY_AGENT_NAME", "the assistant")
        initiative_id = initiative.get("id") or str(uuid.uuid4())
        dedup_subject = initiative.get("entity_id") or initiative_id

        # Rate-limit urgency stays on the 0-1 scale the limiter expects.
        urgency = float(initiative.get("priority", 0.5) or 0.5)

        # Normalize priority: if it's a float <= 1.0, scale to 0-100
        raw_priority = initiative.get("priority", 0.5)
        if isinstance(raw_priority, float) and raw_priority <= 1.0:
            priority = int(raw_priority * 100)
        else:
            priority = int(raw_priority)

        payload = {
            "type": "initiative",
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "payload": {
                "initiative_type": initiative.get("type", "unknown"),
                "title": initiative.get("title", ""),
                "description": initiative.get("description", ""),
                "priority": priority,
                "status": "pending",
                "id": initiative_id,
                "dedup_key": (
                    f"{initiative.get('type', 'unknown')}:{dedup_subject}"
                ),
                "agent_name": agent_name,
                "context": {
                    "trigger": initiative.get("rationale", ""),
                    "suggested_actions": [initiative.get("suggested_action", "review_and_decide")]
                    if initiative.get("suggested_action")
                    else [],
                    "constraints": {},
                    "metadata": {
                        "source": "autonomy_loop",
                        "entity_id": initiative.get("entity_id"),
                        "entity_type": initiative.get("entity_type"),
                    },
                },
                "created_at": initiative.get("generated_at", datetime.now(timezone.utc).isoformat()),
                "expires_at": None,
            },
        }

        # Populate delivery_context for channel routing
        raw_entity_id = initiative.get("entity_id", "")
        initiative_type = initiative.get("type", "")

        # Self-initiatives always route to home channel (v0.11.0)
        is_self_initiative = initiative_type in {
            "subsystem_health", "data_quality", "operational",
            "capability_gap", "knowledge_acquisition", "behavioral_correction",
        }

        channel_hint = initiative.get("channel_hint", "home" if is_self_initiative else "dm")

        if not raw_entity_id or is_self_initiative:
            # System/self initiative — no DM, always home
            person_id = os.environ.get("COLONY_OWNER_CONTACT_ID", "owner")
            user_channel = None
            home_channel = self._channel_registry.resolve("__system__", "home")
        else:
            # Relationship initiatives target a specific person (entity_id IS person_id).
            # All other initiative types (follow_up, health, etc.) target the owner.
            if initiative_type == "relationship":
                person_id = raw_entity_id
            else:
                person_id = os.environ.get("COLONY_OWNER_CONTACT_ID", "owner")

            user_channel = self._channel_registry.resolve(person_id, "dm")
            home_channel = self._channel_registry.resolve(person_id, "home")

        delivery_context = {}
        if user_channel:
            delivery_context["user_chat"] = f"{user_channel.platform}:{user_channel.chat_id}"
        if home_channel:
            delivery_context["home_chat"] = f"{home_channel.platform}:{home_channel.chat_id}"

        if delivery_context:
            payload["delivery_context"] = delivery_context
            payload["channel_hint"] = channel_hint

        # Serialize the payload exactly once and sign the bytes that go on the
        # wire. Sending `json=payload` would let aiohttp re-serialize, so the
        # HMAC could disagree with the receiver's view of the body.
        body_bytes, headers = encode_hermes_webhook(
            payload,
            secret=webhook_secret,
        )

        return {
            "url": hermes_webhook_url,
            "headers": headers,
            "body_bytes": body_bytes,
            "payload": payload,
            "person_id": person_id,
            "urgency": urgency,
            "channel_hint": channel_hint,
            "target": dict(delivery_context),
        }

    async def _prepare_initiative_dispatch_async(
        self, initiative: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Prepare dispatch with an exact async contact route when governed.

        Legacy and owner/default routing retains the synchronous registry
        priority order.  A governed relationship message to a non-owner is
        stricter: it must resolve one verified WhatsApp handle from the
        canonical async contact store and can never fall back to a home chat.
        This resolves identity/transport only; authorization remains at the
        governed producer boundary.
        """
        prep = self._prepare_initiative_dispatch(initiative)
        if not self.governed_gateway_admission_enabled("whatsapp"):
            return prep
        if str(initiative.get("type") or "") != "relationship":
            return prep

        person_id = str(prep.get("person_id") or "").strip()
        configured_owner = str(
            os.environ.get("COLONY_OWNER_CONTACT_ID", "owner") or "owner"
        ).strip()
        if not person_id or person_id in {"owner", configured_owner}:
            return prep

        resolver = getattr(
            self._channel_registry, "resolve_exact_verified_dm", None,
        )
        channel = None
        if callable(resolver):
            try:
                channel = await resolver(person_id, platform="whatsapp")
            except Exception:
                logger.debug(
                    "Governed exact contact route resolution failed",
                    exc_info=True,
                )

        # Non-owner governed outreach has no fallback target.  In particular,
        # a configured home chat must never turn a missing/ambiguous DM into a
        # group disclosure.
        target: Dict[str, str] = {}
        if channel is not None:
            target["user_chat"] = f"{channel.platform}:{channel.chat_id}"
        prep["target"] = target
        payload = prep["payload"]
        if target:
            payload["delivery_context"] = dict(target)
            payload["channel_hint"] = "dm"
        else:
            payload.pop("delivery_context", None)
            payload.pop("channel_hint", None)

        body_bytes, headers = encode_hermes_webhook(
            payload,
            secret=os.environ.get("COLONY_HERMES_WEBHOOK_SECRET", ""),
        )
        prep["body_bytes"] = body_bytes
        prep["headers"] = headers
        return prep

    def preview_initiative(self, initiative: Dict[str, Any]) -> Dict[str, Any]:
        """Resolve where/what an initiative WOULD be delivered, without sending.

        Read-only. Returns the same recipient/target/payload a real
        :meth:`push_initiative` would transmit, for shadow logging and
        operator review.
        """
        prep = self._prepare_initiative_dispatch(initiative)
        return {
            "person_id": prep["person_id"],
            "urgency": prep["urgency"],
            "channel_hint": prep["channel_hint"],
            "target": prep["target"],
            "initiative_type": initiative.get("type", "unknown"),
            "title": initiative.get("title", ""),
            "description": initiative.get("description", ""),
            "rationale": initiative.get("rationale", ""),
            "suggested_action": initiative.get("suggested_action", ""),
            "webhook_payload": prep["payload"],
        }

    async def preview_initiative_async(
        self, initiative: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Async preview used by the guarded autonomous delivery path."""
        prep = await self._prepare_initiative_dispatch_async(initiative)
        return {
            "person_id": prep["person_id"],
            "urgency": prep["urgency"],
            "channel_hint": prep["channel_hint"],
            "target": prep["target"],
            "initiative_type": initiative.get("type", "unknown"),
            "title": initiative.get("title", ""),
            "description": initiative.get("description", ""),
            "rationale": initiative.get("rationale", ""),
            "suggested_action": initiative.get("suggested_action", ""),
            "webhook_payload": prep["payload"],
        }

    async def push_initiative(self, initiative: Dict[str, Any]) -> bool:
        """Push a structured initiative to Hermes via webhook.

        Returns True if Hermes accepted (202), False otherwise.
        """
        try:
            import aiohttp
        except ImportError:
            logger.warning("aiohttp not available — cannot push initiative")
            return False

        prep = self._prepare_initiative_dispatch(initiative)
        hermes_webhook_url = prep["url"]
        headers = prep["headers"]
        body_bytes = prep["body_bytes"]
        priority = prep["payload"]["payload"]["priority"]

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    hermes_webhook_url,
                    data=body_bytes,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10.0),
                ) as resp:
                    if resp.status == 202:
                        logger.info(
                            "Initiative pushed to Hermes: %s (type=%s, priority=%d)",
                            initiative.get("id"),
                            initiative.get("type"),
                            priority,
                        )
                        return True
                    body = await resp.text()
                    logger.warning(
                        "Hermes webhook returned %d: %s",
                        resp.status, body[:200]
                    )
                    return False
        except Exception as exc:
            logger.warning("push_initiative failed: %s", exc)
            return False

    def get_pending(self, gateway_id: str = "", limit: int = 20) -> List[Dict[str, Any]]:
        """Return pending PUSH deliveries for the gateway to send.

        Only returns unsent PUSH channel deliveries. IN_SESSION deliveries are
        fetched separately via get_in_session_context().
        """
        results = []
        for d in self._pending:
            if d.sent:
                continue
            if d.channel != "push":
                continue
            results.append(self._to_dict(d))
            if len(results) >= limit:
                break
        return results

    def mark_sent(self, delivery_id: str) -> bool:
        """Mark a delivery as sent (called by gateway after successful send)."""
        for d in self._pending:
            if d.delivery_id == delivery_id:
                d.sent = True
                self._rate_limiter.record_delivery(d.person_id)
                self._sent.append(d)
                if len(self._sent) > self._sent_max:
                    self._sent = self._sent[-self._sent_max:]
                logger.info("Delivery %s marked sent (person=%s)", delivery_id, d.person_id)
                return True
        logger.debug("mark_sent: delivery %s not found", delivery_id)
        return False

    def get_in_session_context(self, person_id: str) -> Optional[str]:
        """Return pending IN_SESSION deliveries formatted for prompt injection.

        Does NOT mark them as consumed — they survive until explicitly
        acknowledged or expired (see expire_in_session_deliveries).
        """
        now = datetime.now(timezone.utc)
        in_session = [
            d for d in self._pending
            if d.person_id == person_id
            and d.channel == "in_session"
            and not d.sent
            and (now - d.queued_at).total_seconds() < 86400  # 24h max age
        ]
        if not in_session:
            return None

        lines = ["[Things to mention this session]"]
        for d in in_session:
            lines.append(f"• {d.content}")

        return "\n".join(lines)

    def expire_in_session_deliveries(self, max_age_hours: float = 24) -> int:
        """Mark IN_SESSION deliveries older than max_age_hours as sent (expired).

        Returns the count expired.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
        expired = 0
        for d in self._pending:
            if d.channel == "in_session" and not d.sent and d.queued_at < cutoff:
                d.sent = True
                self._rate_limiter.record_delivery(d.person_id)
                expired += 1
        if expired:
            logger.info("Expired %d stale in_session deliveries", expired)
        return expired

    def acknowledge_delivery(self, initiative_id: str) -> bool:
        """Mark any pending delivery matching initiative_id as sent.

        Called when the agent explicitly acknowledges an initiative.
        """
        for d in self._pending:
            if d.initiative_id == initiative_id and not d.sent:
                d.sent = True
                self._rate_limiter.record_delivery(d.person_id)
                logger.info("Delivery %s acknowledged (initiative=%s)", d.delivery_id, initiative_id)
                return True
        return False

    # ------------------------------------------------------------------
    # DIGEST channel
    # ------------------------------------------------------------------

    def get_pending_digest(self, person_id: str) -> List[PendingDelivery]:
        """Return all unsent DIGEST-channel deliveries for ``person_id``."""
        return [
            d for d in self._pending
            if d.person_id == person_id and d.channel == "digest" and not d.sent
        ]

    def pending_digest_recipients(self) -> List[str]:
        """List distinct ``person_id`` values with pending DIGEST items."""
        seen = set()
        ordered: List[str] = []
        for d in self._pending:
            if d.channel != "digest" or d.sent:
                continue
            if d.person_id in seen:
                continue
            seen.add(d.person_id)
            ordered.append(d.person_id)
        return ordered

    def build_digest_bundle(
        self,
        person_id: str,
        *,
        header: str = "Daily digest",
    ) -> Optional[str]:
        """Format this person's pending DIGEST items into a single bundled text
        block. Does not mark anything consumed — pair with ``consume_digest``.
        Items are sorted by urgency descending, then by queue time."""
        items = self.get_pending_digest(person_id)
        if not items:
            return None
        items = sorted(items, key=lambda d: (-d.urgency, d.queued_at))
        lines = [f"[{header}]"]
        for d in items:
            prefix = "\u203c" if d.urgency >= 0.8 else "\u2022"
            lines.append(f"{prefix} {d.content}")
        return "\n".join(lines)

    def consume_digest(self, person_id: str) -> int:
        """Mark all pending DIGEST deliveries for ``person_id`` as sent.

        Returns the count consumed. Each consumed item is also recorded
        against the rate limiter so the digest flush respects per-person
        caps consistently with the other channels.
        """
        consumed = 0
        for d in self._pending:
            if d.person_id == person_id and d.channel == "digest" and not d.sent:
                d.sent = True
                self._rate_limiter.record_delivery(d.person_id)
                self._sent.append(d)
                consumed += 1
        if consumed and len(self._sent) > self._sent_max:
            self._sent = self._sent[-self._sent_max:]
        return consumed

    async def flush_digests_to_gateway(
        self,
        *,
        platform: Optional[str] = None,
        chat_id: Optional[str] = None,
        header: str = "Daily digest",
    ) -> Dict[str, Any]:
        """Bundle each recipient's pending DIGEST items and push the bundle
        to the gateway.

        When ``platform``/``chat_id`` are omitted, the bridge falls back to
        the configured home channel (see ``resolve_home_channel``). If no
        home channel is configured, the flush is a no-op that returns
        ``{"sent": 0, "reason": "no_home_channel"}`` so a scheduler can
        still drain the item count at call time.

        Returns a summary dict: ``{"sent": N, "admitted": N, "skipped": M,
        "recipients": [...], "reason": ...}``.  ``admitted`` is explicitly
        not provider delivery.
        """
        recipients = self.pending_digest_recipients()
        if not recipients:
            return {"sent": 0, "admitted": 0, "skipped": 0, "recipients": []}

        if not platform or not chat_id:
            home = self.resolve_home_channel()
            if home is None:
                return {
                    "sent": 0,
                    "admitted": 0,
                    "skipped": len(recipients),
                    "recipients": recipients,
                    "reason": "no_home_channel",
                }
            platform = platform or home["platform"]
            chat_id = chat_id or home["chat_id"]

        sent = 0
        admitted = 0
        skipped = 0
        for person_id in recipients:
            bundle = self.build_digest_bundle(person_id, header=header)
            if not bundle:
                continue
            digest_source_ids = sorted(
                delivery.delivery_id
                for delivery in self.get_pending_digest(person_id)
            )
            digest_identity = hashlib.sha256(
                json.dumps(
                    digest_source_ids,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            outcome = await self.push_to_gateway(
                platform=platform,
                chat_id=chat_id,
                message=bundle,
                source="digest",
                delivery_id="digest:" + digest_identity,
                source_id="digest:" + digest_identity,
            )
            if bool(outcome) and getattr(
                outcome, "provider_delivered", bool(outcome)
            ) is True:
                self.consume_digest(person_id)
                sent += 1
            elif bool(outcome):
                # Admission to a governed approval/dispatch queue is durable
                # work, but it is not provider delivery and must not consume
                # the delivery-rate budget or pending digest evidence.
                admitted += 1
            else:
                skipped += 1
        return {
            "sent": sent,
            "admitted": admitted,
            "skipped": skipped,
            "recipients": recipients,
        }

    def purge_sent(self) -> int:
        """Remove sent deliveries from the pending queue. Returns count purged."""
        before = len(self._pending)
        self._pending = [d for d in self._pending if not d.sent]
        return before - len(self._pending)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _to_dict(d: PendingDelivery) -> Dict[str, Any]:
        return {
            "delivery_id": d.delivery_id,
            "person_id": d.person_id,
            "content": d.content,
            "channel": d.channel,
            "urgency": d.urgency,
            "source": d.source,
            "initiative_id": d.initiative_id,
            "queued_at": d.queued_at.isoformat(),
            "metadata": d.metadata,
        }
