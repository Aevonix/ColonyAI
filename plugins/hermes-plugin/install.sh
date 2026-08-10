#!/usr/bin/env bash
# Install the governed Colony general plugin into a Hermes home.
# Usage: ./install.sh [--force] [--memory]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
PLUGIN_DIR="$HERMES_HOME/plugins/colony"
FORCE=0
INSTALL_MEMORY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --force)  FORCE=1; shift ;;
    --memory) INSTALL_MEMORY=1; shift ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

if [[ ! -d "$HERMES_HOME" ]]; then
  echo "Hermes home not found at $HERMES_HOME" >&2
  exit 1
fi

STAMP="$(date +%Y%m%d%H%M%S)"
if [[ -d "$PLUGIN_DIR" && "$FORCE" -eq 0 ]]; then
  BACKUP="$PLUGIN_DIR.backup.$STAMP"
  cp -R "$PLUGIN_DIR" "$BACKUP"
  echo "Backed up the existing plugin to $BACKUP"
fi

mkdir -p "$PLUGIN_DIR"
chmod 700 "$PLUGIN_DIR"
for name in __init__.py client.py events.py slash.py plugin.yaml; do
  cp "$SCRIPT_DIR/$name" "$PLUGIN_DIR/$name"
  chmod 600 "$PLUGIN_DIR/$name"
done

if [[ "$INSTALL_MEMORY" -eq 1 ]]; then
  MEMORY_DIR="$HERMES_HOME/plugins/colony-memory"
  MEMORY_SRC="$SCRIPT_DIR/../colony-memory"
  mkdir -p "$MEMORY_DIR"
  chmod 700 "$MEMORY_DIR"
  for name in provider.py __init__.py plugin.yaml; do
    cp "$MEMORY_SRC/$name" "$MEMORY_DIR/$name"
    chmod 600 "$MEMORY_DIR/$name"
  done
  if [[ -f "$MEMORY_SRC/SKILL.md" ]]; then
    cp "$MEMORY_SRC/SKILL.md" "$MEMORY_DIR/"
    chmod 600 "$MEMORY_DIR/SKILL.md"
  fi
fi

# Always replace legacy effect-worker paths with inert compatibility targets.
# Existing scheduled invocations then fail visibly without claiming work or
# posting a webhook.  Preserve each previous script for explicit rollback.
SCRIPTS_DIR="$HERMES_HOME/scripts"
mkdir -p "$SCRIPTS_DIR"
for name in colony-initiative-poller.py colony-queue-worker.py; do
  target="$SCRIPTS_DIR/$name"
  if [[ -f "$target" ]]; then
    cp -p "$target" "$target.pre-governance.$STAMP"
  fi
  cp "$SCRIPT_DIR/poller/$name" "$target"
  chmod +x "$target"
done

echo "Installed governed Colony plugin at $PLUGIN_DIR"
echo "Legacy effect-worker paths are inert; remove their old scheduled entries."
echo "Enable the Colony plugin and canonical Colony memory provider in Hermes config."
