#!/usr/bin/env bash
# Install Colony Hermes integration suite
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLUGIN_ROOT="${HOME}/.hermes/plugins"

echo "Installing Colony Hermes integration suite..."
echo ""

# 1. Memory provider
MEM_DIR="${PLUGIN_ROOT}/memory/colony"
mkdir -p "$MEM_DIR"
cp "$SCRIPT_DIR/__init__.py" "$MEM_DIR/__init__.py"
cp "$SCRIPT_DIR/provider.py" "$MEM_DIR/provider.py"
cp "$SCRIPT_DIR/plugin.yaml" "$MEM_DIR/plugin.yaml"
cp "$SCRIPT_DIR/cli.py" "$MEM_DIR/cli.py"
cp "$SCRIPT_DIR/SKILL.md" "$MEM_DIR/SKILL.md"
echo "  ✓ Memory provider  →  ${MEM_DIR}"

# 2. Context engine
CTX_DIR="${PLUGIN_ROOT}/context_engine/colony"
if [ -d "$SCRIPT_DIR/../hermes-context" ]; then
    mkdir -p "$CTX_DIR"
    cp "$SCRIPT_DIR/../hermes-context/__init__.py" "$CTX_DIR/__init__.py"
    cp "$SCRIPT_DIR/../hermes-context/engine.py" "$CTX_DIR/engine.py" 2>/dev/null || true
    cp "$SCRIPT_DIR/../hermes-context/plugin.yaml" "$CTX_DIR/plugin.yaml" 2>/dev/null || true
    echo "  ✓ Context engine   →  ${CTX_DIR}"
else
    echo "  ⚠ Context engine not found, skipping"
fi

# 3. General plugin (events, tools, slash commands)
GEN_DIR="${PLUGIN_ROOT}/colony"
if [ -d "$SCRIPT_DIR/../hermes-plugin" ]; then
    mkdir -p "$GEN_DIR"
    cp "$SCRIPT_DIR/../hermes-plugin/__init__.py" "$GEN_DIR/__init__.py"
    cp "$SCRIPT_DIR/../hermes-plugin/client.py" "$GEN_DIR/client.py" 2>/dev/null || true
    cp "$SCRIPT_DIR/../hermes-plugin/events.py" "$GEN_DIR/events.py" 2>/dev/null || true
    cp "$SCRIPT_DIR/../hermes-plugin/slash.py" "$GEN_DIR/slash.py" 2>/dev/null || true
    cp "$SCRIPT_DIR/../hermes-plugin/plugin.yaml" "$GEN_DIR/plugin.yaml" 2>/dev/null || true
    echo "  ✓ General plugin   →  ${GEN_DIR}"
else
    echo "  ⚠ General plugin not found, skipping"
fi

echo ""
echo "Add to ~/.hermes/config.yaml:"
echo ""
echo "  memory:"
echo "    provider: colony"
echo "    config:"
echo "      url: \"http://127.0.0.1:7777\""
echo "      api_key: \"\${COLONY_API_KEY}\""
echo "      contact_id: \"default\""
echo ""
echo "  plugins:"
echo "    colony: {}"
echo ""
echo "Restart Hermes after editing config."
