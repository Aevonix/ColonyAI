#!/bin/bash
# Periodic Colony Doctor — validates the Hermes+Colony integration and alerts the
# home/ops channel ONLY on a regression (exit!=0) or a Hermes version change.
# Generic: no agent-specific assumptions. Wired via launchd (every 6h + at load).
export PATH="$HOME/.local/bin:$HOME/.hermes/hermes-agent/venv/bin:$PATH"
PY="$HOME/.hermes/hermes-agent/venv/bin/python"
DOCTOR="$HOME/.hermes/scripts/colony-doctor.py"
LOG="$HOME/.hermes/logs/colony-doctor.log"
WA="$(grep '^WHATSAPP_HOME_CHANNEL=' "$HOME/.hermes/.env" 2>/dev/null | cut -d= -f2-)"

OUT="$("$PY" "$DOCTOR" 2>&1)"; RC=$?
CHANGED=$(printf '%s' "$OUT" | grep -c "HERMES VERSION CHANGED")
{ echo "=== $(date '+%Y-%m-%d %H:%M:%S') rc=$RC changed=$CHANGED ==="; printf '%s\n' "$OUT" | tail -4; } >> "$LOG" 2>&1

if [ "$RC" -ne 0 ] || [ "$CHANGED" -gt 0 ]; then
  SUMMARY="$(printf '%s' "$OUT" | grep -E '❌|⚠️|RESULT:|CHANGED' | head -18)"
  MSG="$(printf '🩺 Colony Doctor alert\n\n%s\n\nRun: ~/.hermes/scripts/colony-doctor.py' "$SUMMARY")"
  [ -n "$WA" ] && hermes send -t "whatsapp:${WA}" "$MSG" >/dev/null 2>&1
fi
exit "$RC"
