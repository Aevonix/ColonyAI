#!/usr/bin/env bash
# Periodic read-only Colony Doctor wrapper. It logs locally and never sends.

set -u
export PATH="$HOME/.local/bin:$HOME/.hermes/hermes-agent/venv/bin:$PATH"
PY="$HOME/.hermes/hermes-agent/venv/bin/python"
DOCTOR="$HOME/.hermes/scripts/colony-doctor.py"
LOG="$HOME/.hermes/logs/colony-doctor.log"

OUT="$("$PY" "$DOCTOR" 2>&1)"
RC=$?
{
  echo "=== $(date '+%Y-%m-%d %H:%M:%S') rc=$RC ==="
  printf '%s\n' "$OUT" | tail -20
} >> "$LOG" 2>&1
exit "$RC"
