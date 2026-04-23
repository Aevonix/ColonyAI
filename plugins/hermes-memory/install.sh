#!/usr/bin/env bash
# Install Colony memory provider plugin for Hermes
set -euo pipefail

PLUGIN_DIR="${HOME}/.hermes/plugins/memory/colony"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "Installing Colony memory provider plugin..."

mkdir -p "$PLUGIN_DIR"
cp "$SCRIPT_DIR/__init__.py" "$PLUGIN_DIR/__init__.py"
cp "$SCRIPT_DIR/provider.py" "$PLUGIN_DIR/provider.py"
cp "$SCRIPT_DIR/SKILL.md" "$PLUGIN_DIR/SKILL.md"

echo "Installed to $PLUGIN_DIR"
echo ""
echo "Add to ~/.hermes/config.yaml:"
echo ""
echo "  memory:"
echo "    provider: colony"
echo "    config:"
echo "      url: \"http://127.0.0.1:7777\""
echo "      api_key: \"\${COLONY_API_KEY}\""
echo "      contact_id: \"default\""
