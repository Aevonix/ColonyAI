"""Read a deployment-selected backup attempt receipt, without running a backup.

The producer owns capture and publication. A receipt reports capture completion,
not archive verification, restore readiness, or coverage of other destinations.
"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re


MAX_RECEIPT_BYTES = 65536
MAX_AGE_DAYS = 7  # Preserve the existing operational backup review interval.


def _read(path):
    with path.open('rb') as stream:
        raw = stream.read(MAX_RECEIPT_BYTES + 1)
    if len(raw) > MAX_RECEIPT_BYTES:
        raise ValueError('receipt exceeds bound')
    return raw, json.loads(raw)


def _time(value, now):
    if not isinstance(value, str):
        raise ValueError('missing timestamp')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None or parsed > now:
        raise ValueError('timestamp is naive or in the future')
    return parsed.astimezone(timezone.utc)


def backup_review_task(configured_path: str, now: datetime):
    """Return a scoped review or None for a recent, bound successful receipt.

    Contract v1: schema_version, status, completed_at, captured_at, receipt_path,
    receipt_sha256. A captured attempt references an immutable local receipt by
    relative path and SHA256; a failed attempt has null capture/reference fields.
    Only the receipt bytes are verified here, never archive bytes or restoration.
    """
    path = Path(configured_path).expanduser().absolute()
    task = {
        'entity_id': 'database_backup', 'entity_type': 'backup',
        'evidence_scope': 'configured_backup_receipt', 'evidence_path': str(path),
        'observed_at': now.isoformat(), 'receipt_status': 'unavailable',
        'captured_at': None, 'completed_at': None,
        'receipt_path': None, 'receipt_sha256': None,
        'description': 'Review configured backup evidence: latest attempt receipt is unavailable; capture freshness and recovery coverage are unknown',
    }
    try:
        _, value = _read(path)
        required = {'schema_version', 'status', 'completed_at', 'captured_at', 'receipt_path', 'receipt_sha256'}
        if (not isinstance(value, dict) or not required.issubset(value)
                or value.get('schema_version') != 1):
            raise ValueError('unsupported receipt schema')
        status = value.get('status')
        completed = _time(value.get('completed_at'), now)
        if status == 'captured':
            captured = _time(value.get('captured_at'), now)
            if captured > completed:
                raise ValueError('capture follows completion')
            relative = value.get('receipt_path')
            digest = value.get('receipt_sha256')
            if (not isinstance(relative, str) or not relative
                    or Path(relative).is_absolute()
                    or not isinstance(digest, str)
                    or not re.fullmatch('[0-9a-f]{64}', digest)):
                raise ValueError('missing receipt reference')
            receipt_path = (path.parent / relative).resolve()
            if not receipt_path.is_relative_to(path.parent.resolve()):
                raise ValueError('receipt is outside configured destination')
            raw, receipt = _read(receipt_path)
            if (hashlib.sha256(raw).hexdigest() != digest
                    or not isinstance(receipt, dict)
                    or receipt.get('status') != 'captured'
                    or _time(receipt.get('at'), now) != captured):
                raise ValueError('capture receipt binding mismatch')
            age = (now - captured).total_seconds() / 86400
            if age <= MAX_AGE_DAYS:
                return None
            task.update(
                captured_at=captured.isoformat(), receipt_path=str(receipt_path),
                receipt_sha256=digest, age_days=age,
                description=f'Review configured backup capture: latest completed capture is {age:.0f} days old; recovery readiness is unverified',
            )
        elif status == 'failed':
            if any(value.get(key) is not None for key in ('captured_at', 'receipt_path', 'receipt_sha256')):
                raise ValueError('failed attempt claims completed capture')
            task['description'] = 'Review configured backup capture: latest attempt failed; earlier captures and recovery readiness require inspection'
        else:
            raise ValueError('unsupported attempt status')
        task.update(receipt_status=status, completed_at=completed.isoformat())
    except FileNotFoundError:
        task['receipt_unavailable_reason'] = 'missing'
    except OSError:
        task['receipt_unavailable_reason'] = 'unreadable'
    except (ValueError, TypeError, OverflowError):
        task['receipt_unavailable_reason'] = 'invalid'
    return task
