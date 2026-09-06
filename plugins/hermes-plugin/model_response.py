"""Decode structured model output without changing its application contract."""
import json
import re


def decode_json_response(raw, *, object_pairs_hook=None):
    """Accept JSON with an optional complete Markdown code fence."""
    body = raw.strip()
    fenced = re.fullmatch(r'```(?:json)?[ \t]*\r?\n(.*?)\r?\n```', body, re.DOTALL)
    return json.loads(fenced.group(1) if fenced else body, object_pairs_hook=object_pairs_hook)
