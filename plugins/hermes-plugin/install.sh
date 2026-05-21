#!/usr/bin/env bash
# Colony ↔ Hermes Plugin Installer
# Usage: ./install.sh [--autonomy] [--force] [--memory] [--poller]
#
# Deploys the Colony general plugin into ~/.hermes/plugins/colony/
# and optionally enables the Autonomy Bridge cron job.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
PLUGIN_DIR="$HERMES_HOME/plugins/colony"
FORCE=0
ENABLE_AUTONOMY=0
INSTALL_MEMORY=0
INSTALL_POLLER=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --autonomy) ENABLE_AUTONOMY=1 ; shift ;;
    --force)    FORCE=1 ; shift ;;
    --memory)   INSTALL_MEMORY=1 ; shift ;;
    --poller)   INSTALL_POLLER=1 ; shift ;;
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

# Deploy examples for reference
if [[ -d "$SCRIPT_DIR/examples" ]]; then
  mkdir -p "$PLUGIN_DIR/examples"
  cp "$SCRIPT_DIR/examples/"* "$PLUGIN_DIR/examples/" 2>/dev/null || true
  echo "   Examples deployed to $PLUGIN_DIR/examples"
fi

echo "   Plugin files deployed."

# Deploy memory provider
if [[ "$INSTALL_MEMORY" -eq 1 ]]; then
  MEMORY_DIR="$HERMES_HOME/plugins/colony-memory"
  mkdir -p "$MEMORY_DIR"
  cp "$SCRIPT_DIR/memory_provider/provider.py" "$MEMORY_DIR/"
  cat > "$MEMORY_DIR/__init__.py" << 'EOF'
from .provider import ColonyMemoryProvider
__all__ = ["ColonyMemoryProvider"]
EOF
  cat > "$MEMORY_DIR/plugin.yaml" << 'EOF'
name: colony-memory
description: Colony memory provider for Hermes
entrypoint: provider:ColonyMemoryProvider
EOF
  echo "   Memory provider deployed to $MEMORY_DIR"
fi

# Deploy poller
if [[ "$INSTALL_POLLER" -eq 1 ]]; then
  SCRIPTS_DIR="$HERMES_HOME/scripts"
  mkdir -p "$SCRIPTS_DIR"
  cp "$SCRIPT_DIR/poller/colony-initiative-poller.py" "$SCRIPTS_DIR/"
  chmod +x "$SCRIPTS_DIR/colony-initiative-poller.py"
  echo "   Initiative poller deployed to $SCRIPTS_DIR/colony-initiative-poller.py"

  # Create cron job if hermes CLI is available
  if command -v hermes &>/dev/null; then
    if hermes cron list 2>/dev/null | grep -q "colony-initiative-poller"; then
      echo "   Poller cron job already exists. Skipping creation."
    else
      echo "   Creating poller cron job..."
      hermes cron create \
        --name colony-initiative-poller \
        --schedule 'every 1m' \
        --script colony-initiative-poller.py \
        --no-agent \
        || echo "   ⚠️  Failed to create cron job. Create manually with: hermes cron create --name colony-initiative-poller --schedule 'every 1m' --script colony-initiative-poller.py --no-agent"
    fi
  else
    echo "   Schedule with: hermes cron create --name colony-initiative-poller --schedule 'every 1m' --script colony-initiative-poller.py --no-agent"
  fi
fi

# Check if plugin is enabled
if command -v hermes &>/dev/null; then
  echo ""
  echo "📋 Next steps:"
  echo "   1. Ensure Colony sidecar is running on port 7777"
  echo "   2. Enable the plugin:   hermes plugins enable colony"
  [[ "$INSTALL_MEMORY" -eq 1 ]] && echo "   3. Configure memory:    hermes memory setup colony"
  [[ "$INSTALL_POLLER" -eq 1 ]] && echo "   4. Poller cron job should be active (check: hermes cron list)"
  echo "   5. Aeva worker (v0.13): Set up scripts/aeva_worker.py via cron every 5 min for AGENT_ACTION jobs"
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
