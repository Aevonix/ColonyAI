"""Task queue handler for async email sending."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict

from colony_sidecar.task_queue.handlers.base import JobHandler
from colony_sidecar.task_queue.models import Job

logger = logging.getLogger(__name__)


class SendEmailHandler(JobHandler):
    """Handles 'send_email' jobs asynchronously.

    The retry loop uses asyncio.sleep and offloads the blocking SMTP call
    to a thread so the worker event loop is never blocked.
    """

    job_type = "send_email"

    async def handle(self, job: Job) -> Dict[str, Any]:
        payload = job.payload
        draft_id = payload["draft_id"]
        db_path = payload.get("db_path")

        from colony_sidecar.email.manager import EmailManager
        manager = EmailManager.create(db_path=db_path)

        # Build provider lazily inside the handler
        draft = manager._store.get_draft(draft_id)
        if draft is None:
            raise KeyError(f"Draft not found: {draft_id}")

        account = manager._store.get_account(draft.account_id)
        if account is None:
            raise KeyError(f"Account not found: {draft.account_id}")

        from colony_sidecar.email.manager import _decrypt_config
        from colony_sidecar.email.models import EmailSendPayload
        creds = _decrypt_config(account.connection_config)
        provider = manager._build_provider(account.provider, creds)
        if provider is None:
            raise ValueError(f"Unknown provider: {account.provider}")

        send_payload = EmailSendPayload(
            from_address=(
                f"{account.display_name} <{account.address}>"
                if account.display_name
                else account.address
            ),
            to_addresses=draft.to_addresses,
            cc_addresses=draft.cc_addresses,
            subject=draft.subject,
            body=draft.body,
            reply_to_message_id=draft.in_reply_to,
            references=[draft.in_reply_to] if draft.in_reply_to else [],
        )

        result = None
        for attempt in range(3):
            result = await asyncio.to_thread(provider.send, send_payload)
            if result.success:
                break
            logger.warning(
                "send_email attempt %d failed for draft %s: %s. Retrying in %ds.",
                attempt + 1,
                draft_id,
                result.error,
                2 ** attempt,
            )
            await asyncio.sleep(2 ** attempt)

        if result is None or not result.success:
            error_msg = result.error if result else "No attempt completed"
            logger.error("send_email failed for draft %s: %s", draft_id, error_msg)
            raise RuntimeError(f"Email send failed: {error_msg}")

        logger.info("send_email succeeded for draft %s (msg_id=%s)", draft_id, result.external_id)
        return {"success": True, "external_id": result.external_id}
