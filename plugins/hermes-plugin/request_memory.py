"""Reconcile canonical source erasure at Hermes' supported request boundary.

This filters model requests, not native transcript storage or paraphrases.
The built-in compressor and clean transcript remain owned by Hermes.
"""
from __future__ import annotations

import json
import copy
import hashlib
import logging
import re
import threading
import time
from collections import OrderedDict

from .client import source_message_hash

logger = logging.getLogger(__name__)
_MEMORY = re.compile(r"(?:\n\n)?<memory-context>.*?(?:</memory-context>|$)", re.S)
_STAMP = re.compile(r"\[colony-recall-v1 (\{[^\n]*\})\]\n")
_PACKET = re.compile(r"\[colony-recall-v1 \{[^\n]*\}\]\n.*?(?:\[/colony-recall-v1\]|$)", re.S)
_ERASED = "[An exact conversation source was forgotten.]"
_UNAVAILABLE = "[Earlier context withheld because memory erasure freshness is unavailable.]"


def _content_key(content):
    return hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=True,
                                    separators=(',', ':')).encode()).hexdigest()


def filter_request(request, *, contact_id, watermark, rules, fresh, aliases=None, current_content=None, current_input=None):
    """Keep fresh packets and remove exact evidence, preserving tool structure.

    Hashes include original session and speaker. Trying those retained origins
    also removes exact full-message copies carried into a child/new session;
    this does not attempt substring or semantic paraphrase deletion.
    """
    origins = {}
    for rule in rules:
        origins.setdefault(rule['session_id'], set()).update(rule['message_hashes'])

    def erased(content):
        return any(source_message_hash(session, {'role': role, 'content': content}) in hashes
                   for session, hashes in origins.items() for role in ('user', 'assistant'))

    def packet(match):
        block = match.group()
        stamp = _STAMP.search(block)
        if not fresh:
            return ''
        if stamp is None:
            # Old native api_content had no stamp. Preserve it only while the
            # contact has no canonical erasure, never guess its dependencies.
            return block if watermark == 0 else ''
        try:
            value = json.loads(stamp.group(1))
            valid = (value['contact_id'] == contact_id
                     and type(value['watermark']) is int
                     and value['watermark'] == watermark)
        except (ValueError, KeyError, TypeError):
            valid = False
        return block if valid else ''

    def text_content(text, *, current=False):
        clean = _PACKET.sub('', _MEMORY.sub('', text))
        if not current and erased(clean):
            return _ERASED
        return _PACKET.sub(packet, _MEMORY.sub(packet, text))

    def content(value, *, current=False):
        if current:
            # Preserve the observed input bytes, including any literal markers
            # the person typed. Only native-appended context is recalled data.
            if isinstance(value, str) and isinstance(current_input, str) and value.startswith(current_input):
                return current_input + text_content(value[len(current_input):])
            if isinstance(value, list) and isinstance(current_input, list) and value[:len(current_input)] == current_input:
                suffix = content(value[len(current_input):])
                return current_input + (suffix if isinstance(suffix, list) else [])
            current = False
        original = aliases.get(_content_key(value), value) if origins and aliases else value
        if not current and erased(original):
            return _ERASED
        if isinstance(value, str):
            return text_content(value, current=current)
        if isinstance(value, list):
            # Native multimodal notes can append packet-only text parts. Match
            # the original full list before filtering individual text blocks,
            # otherwise its erased image/data blocks would survive the hash.
            direct = [part for part in value if not (
                isinstance(part, dict) and part.get('type') in ('text', 'input_text', 'output_text')
                and isinstance(part.get('text'), str)
                and (_MEMORY.search(part['text']) or _PACKET.search(part['text']))
                and not _PACKET.sub('', _MEMORY.sub('', part['text'])).strip())]
            if not current and erased(direct):
                return _ERASED
            return [{**part, 'text': text_content(part['text'], current=current)}
                    if isinstance(part, dict) and isinstance(part.get('text'), str) else part
                    for part in value]
        return value

    result = dict(request)
    for key in ('messages', 'input'):
        messages = request.get(key)
        if isinstance(messages, str):
            result[key] = content(messages)
            continue
        if not isinstance(messages, list):
            continue
        # During a brief sidecar outage keep the current turn and its tool
        # results, but never replay historical evidence with unknown freshness.
        latest_user = max((i for i, row in enumerate(messages)
                           if isinstance(row, dict) and row.get('role') == 'user'), default=len(messages))
        retained = []
        for i, original in enumerate(messages):
            if not isinstance(original, dict):
                continue
            row = dict(original)
            if not fresh and i < latest_user and row.get('role') not in ('system', 'developer'):
                continue
            if 'content' in row:
                current = (i == latest_user and current_content is not None
                           and row['content'] == current_content)
                row['content'] = content(row['content'], current=current)
            if 'output' in row:  # Responses API function output
                row['output'] = content(row['output'])
            retained.append(row)
        if not fresh:
            retained.insert(0, {'role': 'system', 'content': _UNAVAILABLE})
        result[key] = retained
    if isinstance(request.get('instructions'), str):
        result['instructions'] = content(request['instructions'])
    return result


class RequestMemory:
    """One bounded feed reconciliation per actual native model request."""

    def __init__(self, client, outbox):
        self.client, self.outbox = client, outbox
        self._lock = threading.Lock()
        self._aliases = OrderedDict()

    def observe(self, scope, messages, *, user_message=None):
        # Native pre_llm_call exposes both clean content and persisted
        # api_content. Retain only their mapping for this active turn, without
        # mutating messages or guessing how other plugins append context.
        aliases = {_content_key(row['api_content']): row.get('content')
                   for row in messages if isinstance(row, dict) and row.get('api_content')}
        # The native hook includes the actual current input separately from
        # history. Only its matching final row may be treated as new evidence.
        # Observe the descriptor without mutation: native composition stamps
        # api_content onto this same row after the hook, before request dispatch.
        current = messages[-1] if (messages and isinstance(messages[-1], dict)
            and messages[-1].get('role') == 'user'
            and user_message is not None and messages[-1].get('content') == user_message
            and scope.valid_participant) else None
        key = (scope.contact_id, scope.task_id, scope.turn_id)
        with self._lock:
            self._aliases[key] = (aliases, current, copy.deepcopy(user_message) if current else None)
            self._aliases.move_to_end(key)
            while len(self._aliases) > 32:
                self._aliases.popitem(last=False)

    def finish(self, *, task_id, turn_id):
        with self._lock:
            for key in list(self._aliases):
                if key[1:] == (task_id, turn_id):
                    del self._aliases[key]

    def __call__(self, request, scope):
        contact = scope.contact_id if scope is not None and scope.valid_participant else ''
        with self._lock:
            observed_key = (contact, scope.task_id, scope.turn_id) if scope else None
            aliases, current, current_input = self._aliases.get(observed_key, ({}, None, None))
            observed = observed_key in self._aliases
        current_content = current.get('api_content', current.get('content')) if current else None
        deadline = time.monotonic() + .25
        watermark, rules, fresh = 0, [], False
        try:
            if contact:
                watermark, rules = self.outbox.erasure_state(contact, deadline_monotonic=deadline)
                for _ in range(4):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    response = self.client.get("/v1/host/memory/sources/erasures",
                        params={'contact_id': contact, 'after': watermark},
                        timeout=remaining, _deadline_monotonic=deadline)
                    response.raise_for_status()
                    page = response.json()
                    self.outbox.apply_erasure_page(contact, page, deadline_monotonic=deadline)
                    watermark, rules = self.outbox.erasure_state(contact, deadline_monotonic=deadline)
                    fresh = (page.get('complete') is True
                             and int(page['head']) == int(page['through']) <= watermark)
                    if fresh:
                        break
        except Exception as error:
            logger.warning('request memory freshness unavailable (%s)', type(error).__name__)
        if watermark and not observed:
            # A missing/evicted pre-turn observation cannot certify enriched
            # historical api_content. Its transcript must not fail open.
            fresh = False
        # Do not raise: Hermes intentionally fails open on middleware errors.
        # Failure returns an explicit reduced request instead of stale history.
        try:
            filtered = filter_request(request, contact_id=contact, watermark=watermark,
                                      rules=rules, fresh=fresh, aliases=aliases,
                                      current_content=current_content, current_input=current_input)
        except Exception:
            filtered = filter_request(request, contact_id=contact, watermark=0, rules=[], fresh=False)
            fresh = False
        return {'request': filtered, 'source': 'colony',
                'reason': 'source_erasure_checked' if fresh else 'source_erasure_unavailable'}
