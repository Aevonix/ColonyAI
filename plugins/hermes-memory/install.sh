#!/usr/bin/env bash
# Install Colony Hermes integration suite
# Works on: macOS, Linux, WSL
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLUGIN_ROOT="${HOME}/.hermes/plugins"

# Detect if running from Colony repo or standalone
REPO_ROOT=""
if [ -d "$SCRIPT_DIR/../hermes-context" ] && [ -d "$SCRIPT_DIR/../hermes-plugin" ]; then
    REPO_ROOT="$SCRIPT_DIR/.."
    echo "Detected Colony repo layout"
else
    # Try to find repo root (2 levels up from plugins/hermes-memory/)
    PARENT_PARENT="$(cd "$SCRIPT_DIR/../.." && pwd)"
    if [ -d "$PARENT_PARENT/plugins/hermes-context" ] && [ -d "$PARENT_PARENT/plugins/hermes-plugin" ]; then
        REPO_ROOT="$PARENT_PARENT/plugins"
        echo "Detected Colony repo layout (nested)"
    fi
fi

echo "Installing Colony Hermes integration suite..."
echo ""

# 1. Memory provider
MEM_DIR="${PLUGIN_ROOT}/memory/colony"
mkdir -p "$MEM_DIR"
cp "$SCRIPT_DIR/__init__.py" "$MEM_DIR/__init__.py"
cp "$SCRIPT_DIR/provider.py" "$MEM_DIR/provider.py"
cp "$SCRIPT_DIR/plugin.yaml" "$MEM_DIR/plugin.yaml"
cp "$SCRIPT_DIR/cli.py" "$MEM_DIR/cli.py" 2>/dev/null || true
cp "$SCRIPT_DIR/SKILL.md" "$MEM_DIR/SKILL.md" 2>/dev/null || true
echo "  ✅ Memory provider  →  ${MEM_DIR}"

# 2. Context engine
if [ -n "$REPO_ROOT" ] && [ -d "$REPO_ROOT/hermes-context" ]; then
    CTX_DIR="${PLUGIN_ROOT}/context_engine/colony"
    mkdir -p "$CTX_DIR"
    cp "$REPO_ROOT/hermes-context/__init__.py" "$CTX_DIR/__init__.py"
    cp "$REPO_ROOT/hermes-context/plugin.yaml" "$CTX_DIR/plugin.yaml"
    echo "  ✅ Context engine   →  ${CTX_DIR}"
else
    echo "  ⚠ Context engine not found, skipping"
fi

# 3. General plugin (events, tools, slash commands)
if [ -n "$REPO_ROOT" ] && [ -d "$REPO_ROOT/hermes-plugin" ]; then
    GEN_DIR="${PLUGIN_ROOT}/colony"
    mkdir -p "$GEN_DIR"
    cp "$REPO_ROOT/hermes-plugin/__init__.py" "$GEN_DIR/__init__.py"
    cp "$REPO_ROOT/hermes-plugin/client.py" "$GEN_DIR/client.py"
    cp "$REPO_ROOT/hermes-plugin/events.py" "$GEN_DIR/events.py"
    cp "$REPO_ROOT/hermes-plugin/slash.py" "$GEN_DIR/slash.py"
    cp "$REPO_ROOT/hermes-plugin/plugin.yaml" "$GEN_DIR/plugin.yaml"
    echo "  ✅ General plugin   →  ${GEN_DIR}"
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
echo "  context_engine: colony"
echo ""
echo "  plugins:"
echo "    colony:"
echo "      url: \"http://127.0.0.1:7777\""
echo "      api_key: \"\${COLONY_API_KEY}\""
echo "      contact_id: \"default\""
echo ""
echo "Restart Hermes after editing config."
