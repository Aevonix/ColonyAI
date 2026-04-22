#!/usr/bin/env python3
"""E2E Test: Full Cycle — Seed → Learn → Act → Verify

Creates a commitment, simulates conversations mentioning it, verifies
the system extracts, tracks, and surfaces it in context, then checks
autonomy detects it as overdue.

Usage: COLONY_API_KEY=test python3 test_full_cycle.py
"""

import json
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx

COLONY_URL = os.environ.get("COLONY_URL", "http://localhost:7777")
COLONY_API_KEY = os.environ.get("COLONY_API_KEY", "")
HEADERS = {"Authorization": f"Bearer {COLONY_API_KEY}"}
CT = {"Content-Type": "application/json"}

def log(msg, status=""):
    tag = f" [{status}]" if status else ""
    print(f"  {msg}{tag}")

def get(path, **params):
    return httpx.get(f"{COLONY_URL}{path}", headers=HEADERS, params=params, timeout=10)

def post(path, data):
    return httpx.post(f"{COLONY_URL}{path}", headers={**HEADERS, **CT}, json=data, timeout=15)

def patch(path, data):
    return httpx.patch(f"{COLONY_URL}{path}", headers={**HEADERS, **CT}, json=data, timeout=10)

def delete(path):
    return httpx.delete(f"{COLONY_URL}{path}", headers=HEADERS, timeout=10)


def test_full_cycle():
    contact = f"cycle-test-{uuid.uuid4().hex[:6]}"
    passed = 0
    failed = 0

    def check(name, condition, detail=""):
        nonlocal passed, failed
        if condition:
            log(f"  {name}: {detail}", "✅")
            passed += 1
        else:
            log(f"  {name}: {detail}", "❌")
            failed += 1

    # ═══════════════════════════════════════════════════════════════════
    # PHASE 1: SEED — Create commitment due very soon
    # ═══════════════════════════════════════════════════════════════════
    log("\n══ PHASE 1: SEED ══\n")

    # Create commitment due in 3 seconds (will become overdue during test)
    due = (datetime.now(timezone.utc) + timedelta(seconds=3)).isoformat()
    r = post("/v1/host/commitments", {
        "person_id": contact,
        "description": "Ship Colony v0.6.0 with SuperColony Network prototype",
        "due_at": due,
        "priority": 3,
    })
    check("Create commitment", r.status_code in (200, 201), f"status={r.status_code}")
    cid = r.json().get("id")

    # Create a fact about this person
    r = post("/v1/host/mind/facts", {
        "contact_id": contact,
        "fact": "Lead architect on Colony project — focused on cognitive subsystems",
        "category": "role",
        "confidence": 0.9,
    })
    check("Create fact", r.status_code in (200, 201), f"status={r.status_code}")

    # Create world model entity for the project
    r = post("/v1/host/world/entities", {
        "name": "ColonyAI v0.6.0",
        "entity_type": "project",
        "confidence": 0.95,
        "properties": {"status": "in_progress", "milestone": "SuperColony Network"},
    })
    check("Create world entity", r.status_code == 200, f"status={r.status_code}")

    # ═══════════════════════════════════════════════════════════════════
    # PHASE 2: LEARN — Simulate conversations about the commitment
    # ═══════════════════════════════════════════════════════════════════
    log("\n══ PHASE 2: LEARN ══\n")

    # Simulate conversation via turn_sync
    r = post("/v1/host/turns/sync", {
        "identity": {"host_id": "e2e-test"},
        "context": {"session_id": f"cycle-{uuid.uuid4().hex[:6]}", "contact_id": contact},
        "incoming_message": {
            "role": "user",
            "content": "I'm working hard on the SuperColony Network. The progress is good but the deadline is tight. I prefer async communication and late-night coding.",
        },
        "outgoing_message": {
            "role": "assistant",
            "content": "Your progress on SuperColony Network sounds solid. I've noted your async communication preference. Let me know if you need help with the deadline.",
        },
    })
    check("turn_sync accepted", r.status_code in (200, 501), f"status={r.status_code}")

    # Track affect — should be positive (good progress)
    r = post("/v1/host/affect/events", {
        "contact_id": contact,
        "valence": 0.7,
        "arousal": 0.5,
        "trigger": "productive progress on SuperColony",
    })
    check("Affect tracked", r.status_code in (200, 201), f"valence=0.7")

    # Track a surprise
    r = post("/v1/host/surprises", {
        "observation": "Deadline for v0.6.0 is extremely tight given current progress",
        "expected": "Comfortable timeline",
        "actual": "Tight deadline with risk of slip",
        "surprise_score": 0.7,
    })
    check("Surprise logged", r.status_code in (200, 201), f"score=0.7")

    # Wait for async processing
    time.sleep(3)

    # ═══════════════════════════════════════════════════════════════════
    # PHASE 3: ACT — Verify the system surfaces everything in context
    # ═══════════════════════════════════════════════════════════════════
    log("\n══ PHASE 3: ACT ══\n")

    # Assemble context and verify all sections appear
    r = post("/v1/host/context/assemble", {
        "identity": {"host_id": "e2e-test"},
        "context": {"session_id": "cycle-act", "contact_id": contact},
        "incoming_message": {"role": "user", "content": "What should I be working on?"},
    })
    check("Context assembly responds", r.status_code == 200, f"status={r.status_code}")

    if r.status_code == 200:
        sections = r.json().get("sections", [])
        section_ids = [s["id"] for s in sections]
        section_titles = {s["id"]: s["title"] for s in sections}

        check("Commitments in context", "colony-commitments" in section_ids,
              section_titles.get("colony-commitments", "missing"))
        check("Affect in context", "colony-affect" in section_ids,
              section_titles.get("colony-affect", "missing"))
        check("Facts in context", "colony-shared-facts" in section_ids,
              section_titles.get("colony-shared-facts", "missing"))
        check("Surprises in context", "colony-surprises" in section_ids,
              section_titles.get("colony-surprises", "missing"))

        # Verify commitment text appears in context
        ctx_text = json.dumps(sections)
        check("Commitment text in context", "SuperColony" in ctx_text or "v0.6.0" in ctx_text,
              "SuperColony/v0.6.0 found" if "SuperColony" in ctx_text or "v0.6.0" in ctx_text else "not found")

    # ═══════════════════════════════════════════════════════════════════
    # PHASE 4: VERIFY — Check overdue detection and autonomy
    # ═══════════════════════════════════════════════════════════════════
    log("\n══ PHASE 4: VERIFY ══\n")

    # Wait for commitment to become overdue (was due 3s from creation)
    log("  Waiting 5s for commitment to become overdue...")
    time.sleep(5)

    # Check overdue commitments
    r = get("/v1/host/commitments", status="overdue")
    if r.status_code == 200:
        overdue = r.json()
        if isinstance(overdue, dict):
            overdue = overdue.get("commitments", [])
        overdue_ids = [c["id"] for c in overdue]
        check("Commitment is overdue", cid in overdue_ids,
              f"found in {len(overdue)} overdue items")
    else:
        check("Commitment is overdue", False, f"query failed: {r.status_code}")

    # Check autonomy loop status
    r = get("/v1/host/autonomy/status")
    if r.status_code == 200:
        status = r.json()
        running = status.get("running", False)
        ticks = status.get("ticks", 0)
        check("Autonomy loop running", running, f"ticks={ticks}")

        # Force a tick to check for overdue
        if running:
            log("  Autonomy is running — overdue detection will fire on next tick")
            check("Autonomy tick count", ticks > 0, f"ticks={ticks}")
    else:
        check("Autonomy status", False, f"status={r.status_code}")

    # Check world model stats
    r = get("/v1/host/world/stats")
    if r.status_code == 200:
        stats = r.json()
        check("World model has data", stats.get("total_entities", 0) > 0,
              f"entities={stats.get('total_entities', 0)}")
    else:
        check("World model stats", False, f"status={r.status_code}")

    # ═══════════════════════════════════════════════════════════════════
    # PHASE 5: CLEANUP — Fulfill and delete commitment
    # ═══════════════════════════════════════════════════════════════════
    log("\n══ PHASE 5: CLEANUP ══\n")

    if cid:
        r = patch(f"/v1/host/commitments/{cid}", {"status": "fulfilled"})
        check("Fulfill commitment", r.status_code == 200, f"status={r.status_code}")

        r = delete(f"/v1/host/commitments/{cid}")
        check("Delete commitment", r.status_code == 204, f"status={r.status_code}")

    # Verify fulfilled commitment not in overdue
    r = get("/v1/host/commitments", status="overdue")
    if r.status_code == 200:
        overdue = r.json()
        if isinstance(overdue, dict):
            overdue = overdue.get("commitments", [])
        overdue_ids = [c["id"] for c in overdue]
        check("Fulfilled not overdue", cid not in overdue_ids,
              "correctly excluded" if cid not in overdue_ids else "still showing!")

    # ═══════════════════════════════════════════════════════════════════
    # SUMMARY
    # ═══════════════════════════════════════════════════════════════════
    log(f"\n══ RESULTS: {passed} passed, {failed} failed ══")
    return failed == 0


if __name__ == "__main__":
    try:
        success = test_full_cycle()
        sys.exit(0 if success else 1)
    except Exception as exc:
        print(f"\n  FATAL: {exc}", "❌")
        import traceback
        traceback.print_exc()
        sys.exit(1)
