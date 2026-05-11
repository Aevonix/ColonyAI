#!/usr/bin/env bash
# Colony ↔ Hermes Plugin Installer
# Usage: ./install.sh [--autonomy] [--force]
#
# Deploys the Colony general plugin into ~/.hermes/plugins/colony/
# and optionally enables the Autonomy Bridge cron job.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
PLUGIN_DIR="$HERMES_HOME/plugins/colony"
FORCE=0
ENABLE_AUTONOMY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --autonomy) ENABLE_AUTONOMY=1 ; shift ;;
    --force)    FORCE=1 ; shift ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

echo "🚀 Colony ↔ Hermes Plugin Installer"
echo "   Target: $PLUGIN_DIR"

# Check Hermes is installed
if [[ ! -d "$HERMES_HOME" ]]; then
  echo "❌ Hermes home not found at $HERMES_HOME"
  echo "   Install Hermes first: https://github.com/nousresearch/hermes-agent"
  exit 1
fi

# Backup existing plugin if present
if [[ -d "$PLUGIN_DIR" && "$FORCE" -eq 0 ]]; then
  BACKUP="$PLUGIN_DIR.backup.$(date +%Y%m%d%H%M%S)"
  echo "   Backing up existing plugin to $BACKUP"
  cp -R "$PLUGIN_DIR" "$BACKUP"
fi

# Deploy plugin files
mkdir -p "$PLUGIN_DIR"
cp "$SCRIPT_DIR/__init__.py" "$PLUGIN_DIR/"
cp "$SCRIPT_DIR/client.py" "$PLUGIN_DIR/"
cp "$SCRIPT_DIR/slash.py" "$PLUGIN_DIR/"
cp "$SCRIPT_DIR/events.py" "$PLUGIN_DIR/"
cp "$SCRIPT_DIR/plugin.yaml" "$PLUGIN_DIR/"

echo "   Plugin files deployed."

# Check if plugin is enabled
if command -v hermes &>/dev/null; then
  echo ""
  echo "📋 Next steps:"
  echo "   1. Ensure Colony sidecar is running on port 7777"
  echo "   2. Enable the plugin:   hermes plugins enable colony"
  echo "   3. Start a new session: hermes"
  echo ""

  # Autonomy wizard prompt
  if [[ "$ENABLE_AUTONOMY" -eq 1 ]]; then
    echo "✅ Autonomy flag set — enabling background initiative handling..."
    echo "   (Run '/colony autonomy enable' in Hermes if this fails)"
  else
    echo "🤖 Autonomy Bridge:"
    echo "   Colony can run autonomously on your behalf — checking for"
    echo "   relationship reminders and tasks every 15 minutes."
    echo ""
    if [[ -t 0 ]]; then
      read -rp "   Enable autonomous initiative handling now? [y/N] " response
      if [[ "$response" =~ ^[Yy]$ ]]; then
        ENABLE_AUTONOMY=1
      fi
    fi
  fi

  if [[ "$ENABLE_AUTONOMY" -eq 1 ]]; then
    echo "   Creating autonomy cron job..."
    # We can't easily create the job from here because it requires the Hermes
    # runtime context. Instead, we print the command the user should run.
    echo ""
    echo "   ⚡ Run this inside Hermes to activate:"
    echo "      /colony autonomy enable"
    echo ""
    echo "   Or from CLI:"
    echo "      hermes -c '/colony autonomy enable'"
  fi
else
  echo "⚠️  'hermes' command not found in PATH."
  echo "   Add Hermes to your PATH, then run:"
  echo "      hermes plugins enable colony"
fi

echo ""
echo "✅ Installation complete."
