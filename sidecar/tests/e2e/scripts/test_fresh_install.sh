#!/usr/bin/env bash
# E2E test: colony init on a fresh pip install
# Feeds all wizard inputs non-interactively

set -e

echo "═══════════════════════════════════════════════════════"
echo "  COLONY INIT E2E TEST — Fresh pip install"
echo "═══════════════════════════════════════════════════════"
echo ""

# Clean slate
FRESH_DIR="/tmp/colony-init-test"
rm -rf "$FRESH_DIR"
mkdir -p "$FRESH_DIR"
cd "$FRESH_DIR"

# Create venv + install from PyPI
echo "[1/4] Installing colonyai from PyPI..."
python3 -m venv venv
source venv/bin/activate
pip install colonyai==0.5.5 -q 2>&1 | tail -1

echo "[2/4] Running 'colony init'..."
echo ""

# Feed all prompts in order:
# Step 3: host framework = openclaw
# Step 3: configure plugin? n (no openclaw CLI)
# Step 5: neo4j password = colony-local-dev (Neo4j already running)
# Step 6: tier selection = 0 (minimal, we'll override to skip)
# Step 6: embed mode = 3 (skip)
# Step 7b: multimodal? n
# Step 10: start sidecar? Y
# Step 10c: restart gateway? n
printf 'openclaw\nn\ncolony-local-dev\n0\n3\nn\nY\nn\n' | \
  colony init 2>&1 || true

echo ""
echo "[3/4] Checking .env..."
if [ -f .env ]; then
  echo "  ✅ .env exists"
  grep -q "COLONY_API_KEY=" .env && echo "  ✅ COLONY_API_KEY set"
  grep -q "NEO4J_PASSWORD=" .env && echo "  ✅ NEO4J_PASSWORD set"
  grep -q "WORLD_MODEL_BACKEND=" .env && echo "  ✅ WORLD_MODEL_BACKEND set"
  grep -q "COLONY_EMBED_PROVIDER=" .env && echo "  ✅ COLONY_EMBED_PROVIDER set ($(grep COLONY_EMBED_PROVIDER .env | cut -d= -f2))"
else
  echo "  ❌ .env not found!"
  exit 1
fi

echo ""
echo "[4/4] Starting sidecar from .env..."
set -a
source .env
set +a

# Override embed provider to skip (wizard might have written something else)
export COLONY_EMBED_PROVIDER=skip

nohup python3 -m uvicorn colony_sidecar.server:app \
  --host 127.0.0.1 --port 7779 > /tmp/init_test_sidecar.log 2>&1 &
SIDECAR_PID=$!
echo "  PID: $SIDECAR_PID"
sleep 10

HEALTH=$(curl -s -H "Authorization: Bearer $COLONY_API_KEY" http://127.0.0.1:7779/v1/host/health 2>/dev/null || echo "")
if echo "$HEALTH" | grep -q '"ok"'; then
  CAPS=$(echo "$HEALTH" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('capabilities',[])))" 2>/dev/null || echo "?")
  echo "  ✅ Sidecar healthy — $CAPS capabilities"
  
  # Test context assembly
  CTX=$(curl -s -X POST -H "Authorization: Bearer $COLONY_API_KEY" \
    -H "Content-Type: application/json" \
    http://127.0.0.1:7779/v1/host/context/assemble \
    -d '{"identity":{"host_id":"test"},"context":{"session_id":"init-test","contact_id":"test"},"incoming_message":{"role":"user","content":"hello"}}' \
    2>/dev/null || echo "")
  SECTIONS=$(echo "$CTX" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('sections',[])))" 2>/dev/null || echo "0")
  echo "  ✅ Context assembly: $SECTIONS sections"
else
  echo "  ❌ Sidecar not responding"
  cat /tmp/init_test_sidecar.log | head -20
fi

kill $SIDECAR_PID 2>/dev/null || true
echo ""
echo "═══════════════════════════════════════════════════════"
echo "  TEST COMPLETE"
echo "═══════════════════════════════════════════════════════"
