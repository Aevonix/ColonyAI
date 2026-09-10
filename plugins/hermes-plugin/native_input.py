"""Transient clean input from the native synchronous memory-provider callback.

The model request is not a source of sender identity or clean human text.
This carrier exists only in the current native execution context and is bound
to one exact request turn before it can repair a skipped observer callback.
"""
from contextvars import ContextVar
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class NativeInput:
    session_id: str
    platform: str
    sender_id: str
    message: str
    bound: tuple[str, str] | None = None


_CURRENT = ContextVar('colony_native_clean_input', default=None)


def _transport():
    from gateway.session_context import get_session_env
    return tuple(str(get_session_env('HERMES_SESSION_'+name, '') or '')
                 for name in ('ID', 'PLATFORM', 'USER_ID'))


def capture(message, *, session_id='', platform=''):
    _CURRENT.set(None)
    if not isinstance(message, str) or not message:
        return
    try:
        session, transport, sender = _transport()
        session, transport = session or session_id, transport or platform
        if session and transport:
            _CURRENT.set(NativeInput(session, transport, sender, message))
    except Exception:
        pass


def for_request(request, **context):
    value = _CURRENT.get()
    if value is None:
        return None
    try:
        from agent.delegation_context import is_delegated_child_process_context
        if is_delegated_child_process_context():
            return None
        session, transport, sender = _transport()
    except Exception:
        return None
    turn = (str(context.get('task_id') or ''), str(context.get('turn_id') or ''))
    if (not all(turn) or value.bound not in (None, turn)
            or value.session_id != context.get('session_id')
            or value.platform != context.get('platform')
            or (session and session != value.session_id)
            or (transport and transport != value.platform) or sender != value.sender_id):
        return None
    _CURRENT.set(replace(value, bound=turn))
    # Never strip a guessed prefix or treat a caller-authored memory marker as
    # native evidence. Only the independently observed original input can be
    # the clean side of this exact current-row mapping.
    rows = request.get('messages', request.get('input', []))
    if not isinstance(rows, list):
        return None
    current = next((row for row in reversed(rows) if isinstance(row, dict)
                    and row.get('role') == 'user'), None)
    enriched = current.get('content') if current else None
    if not isinstance(enriched, str) or not enriched.startswith(value.message):
        return None
    return {'session_id': value.session_id, 'task_id': turn[0], 'turn_id': turn[1],
            'platform': value.platform, 'sender_id': value.sender_id,
            'user_message': value.message,
            'conversation_history': [{'role': 'user', 'content': value.message, 'api_content': enriched}]}
