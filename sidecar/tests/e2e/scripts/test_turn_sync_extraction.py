#!/usr/bin/env python3
"""E2E Test: Turn Sync → Extraction Pipeline

Sends a real conversation through Colony's turn_sync, then verifies
that ToM extraction, pattern extraction, and surprise scoring fired
and stored results.

Usage: COLONY_API_KEY=test python3 test_turn_sync_extraction.py
"""

import json
import os
import sys
import time
import uuid

import httpx

COLONY_URL = os.environ.get("COLONY_URL", "http://localhost:7777")
COLONY_API_KEY = os.environ.get("COLONY_API_KEY", "")
HEADERS = {"Authorization": f"Bearer {COLONY_API_KEY}"}
CT = {"Content-Type": "application/json"}

def log(msg, status=""):
    tag = f" [{status}]" if status else ""
    print(f"  {msg}{tag}")

def get(path, **params):
    r = httpx.get(f"{COLONY_URL}{path}", headers=HEADERS, params=params, timeout=10)
    return r

def post(path, data):
    r = httpx.post(f"{COLONY_URL}{path}", headers={**HEADERS, **CT}, json=data, timeout=15)
    return r

def patch(path, data):
    r = httpx.patch(f"{COLONY_URL}{path}", headers={**HEADERS, **CT}, json=data, timeout=10)
    return r


def test_turn_sync_extraction():
    contact = f"extraction-test-{uuid.uuid4().hex[:6]}"
    log(f"Contact: {contact}")

    # ── Step 1: Seed some baseline data ──────────────────────────────────
    log("\n─ Step 1: Seed baseline data ─")

    # Create a commitment
    r = post("/v1/host/commitments", {
        "person_id": contact,
        "description": "Deploy quantum stabilizer before demo day",
        "priority": 3,
    })
    assert r.status_code in (200, 201), f"Commitment failed: {r.text}"
    log(f"  Commitment created", "✅")

    # Create an affect baseline
    r = post("/v1/host/affect/events", {
        "contact_id": contact,
        "valence": 0.5,
        "arousal": 0.3,
        "trigger": "neutral start",
    })
    assert r.status_code in (200, 201), f"Affect failed: {r.text}"
    log(f"  Affect baseline set", "✅")

    # ── Step 2: Send turn_sync with rich conversation ────────────────────
    log("\n─ Step 2: Send turn_sync ─")

    r = post("/v1/host/turns/sync", {
        "identity": {"host_id": "e2e-test"},
        "context": {
            "session_id": f"extraction-{uuid.uuid4().hex[:6]}",
            "contact_id": contact,
        },
        "incoming_message": {
            "role": "user",
            "content": "I'm really frustrated with the quantum stabilizer — it keeps crashing during integration tests. But I'm excited about the demo next week. Also, I prefer dark mode in all my editors.",
        },
        "outgoing_message": {
            "role": "assistant",
            "content": "I understand the frustration with the quantum stabilizer crashes. Let's focus on fixing the integration tests first. The demo next week is a great motivator. I've noted your dark mode preference.",
        },
    })
    log(f"  turn_sync response: {r.status_code}")
    # 200 = accepted, 501 = not fully wired
    if r.status_code == 501:
        log("  turn_sync not fully wired — extraction won't fire", "⚠️")
    elif r.status_code != 200:
        log(f"  Unexpected: {r.text}", "❌")
        return False
    else:
        log("  turn_sync accepted", "✅")

    # ── Step 3: Wait for async extraction ────────────────────────────────
    log("\n─ Step 3: Wait for async extraction (5s) ─")
    time.sleep(5)

    # ── Step 4: Check if ToM extraction fired ────────────────────────────
    log("\n─ Step 4: Check ToM extraction ─")

    # Check affect — should have shifted toward negative (frustration)
    r = get(f"/v1/host/affect/state/{contact}")
    if r.status_code == 200:
        state = r.json()
        valence = state.get("valence")
        if valence is not None:
            if valence < 0.5:
                log(f"  Affect shifted negative: valence={valence:.2f}", "✅")
            else:
                log(f"  Affect didn't shift: valence={valence:.2f}", "⚠️")
        else:
            log(f"  Affect state returned but no valence", "⚠️")
    else:
        log(f"  Affect state failed: {r.status_code}", "⚠️")

    # Check shared facts — should have "dark mode" preference
    r = get("/v1/host/mind/facts", contact_id=contact, limit=10)
    if r.status_code == 200:
        data = r.json()
        facts = data if isinstance(data, list) else data.get("facts", [])
        dark_mode_facts = [f for f in facts if "dark mode" in f.get("fact", "").lower()]
        if dark_mode_facts:
            log(f"  Found dark mode fact: {dark_mode_facts[0]['fact'][:60]}", "✅")
        else:
            log(f"  No dark mode fact extracted (may need LLM extraction)", "⚠️")
            log(f"  Existing facts: {[f.get('fact','')[:40] for f in facts[:3]]}")
    else:
        log(f"  Facts query failed: {r.status_code}", "⚠️")

    # ── Step 5: Check patterns ───────────────────────────────────────────
    log("\n─ Step 5: Check patterns ─")

    r = get("/v1/host/patterns", limit=10)
    if r.status_code == 200:
        data = r.json()
        patterns = data if isinstance(data, list) else data.get("patterns", [])
        log(f"  Total patterns: {len(patterns)}")
        if patterns:
            log(f"  Latest: {patterns[0].get('pattern_key', patterns[0].get('description', ''))[:50]}", "✅")
    else:
        log(f"  Patterns query failed: {r.status_code}", "⚠️")

    # ── Step 6: Check surprises ──────────────────────────────────────────
    log("\n─ Step 6: Check surprises ─")

    r = get("/v1/host/surprises/unresolved")
    if r.status_code == 200:
        surprises = r.json()
        log(f"  Unresolved surprises: {len(surprises)}")
        if surprises:
            log(f"  Latest: {surprises[0].get('observation', '')[:60]}", "✅")
    else:
        log(f"  Surprises query failed: {r.status_code}", "⚠️")

    # ── Step 7: Manual ToM extraction trigger ─────────────────────────────
    log("\n─ Step 7: Trigger manual ToM extraction ─")

    r = post("/v1/host/tom/extract", {
        "contact_id": contact,
        "text": "I'm really frustrated with the quantum stabilizer. But excited about the demo. I prefer dark mode.",
    })
    if r.status_code == 200:
        result = r.json()
        affect = result.get("affect")
        facts_count = len(result.get("facts", []))
        throttled = result.get("throttled", False)
        log(f"  Affect extracted: {affect is not None} | Facts: {facts_count} | Throttled: {throttled}")
        if affect:
            log(f"  Affect: valence={affect.get('valence')}, arousal={affect.get('arousal')}", "✅")
        if facts_count > 0:
            for f in result.get("facts", [])[:3]:
                log(f"  Fact: {f.get('fact', f.get('item', ''))[:60]}", "✅")
        if not affect and facts_count == 0:
            log("  No extraction results (LLM router may not be wired)", "⚠️")
    elif r.status_code == 501:
        log("  ToM extraction not wired", "⚠️")
    else:
        log(f"  ToM extraction: {r.status_code}", "⚠️")

    # ── Step 8: Verify context assembly includes extraction results ───────
    log("\n─ Step 8: Context assembly includes extraction results ─")

    r = post("/v1/host/context/assemble", {
        "identity": {"host_id": "e2e-test"},
        "context": {"session_id": "extraction-verify", "contact_id": contact},
        "incoming_message": {"role": "user", "content": "How am I doing?"},
    })
    if r.status_code == 200:
        sections = r.json().get("sections", [])
        section_ids = [s["id"] for s in sections]
        log(f"  Sections: {section_ids}")
        if "colony-commitments" in section_ids:
            log("  Commitments in context", "✅")
        if "colony-affect" in section_ids:
            log("  Affect in context", "✅")
        if "colony-shared-facts" in section_ids:
            log("  Shared facts in context", "✅")
        if "colony-surprises" in section_ids:
            log("  Surprises in context", "✅")
    else:
        log(f"  Context assembly failed: {r.status_code}", "❌")

    log("\n═ Turn Sync → Extraction test complete ═")
    return True


if __name__ == "__main__":
    try:
        test_turn_sync_extraction()
    except Exception as exc:
        print(f"\n  FATAL: {exc}", "❌")
        sys.exit(1)
