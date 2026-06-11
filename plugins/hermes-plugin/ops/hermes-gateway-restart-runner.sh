#!/bin/bash
# Canonical external Hermes-gateway restart — run by launchd, NOT by the gateway,
# so it survives the restart it performs. Stops the gateway, clears any orphaned
# bridge, starts it fresh, verifies, writes a status file, and on success WAKES
# the agent two reliable ways: an instant `hermes send` "I'm back" to the owner
# DM, plus a resume-marker file the (proven) 10-min worker cron picks up to
# actually resume an in-progress task. $1 = status file.
UID_N="$(id -u)"
GW_LABEL="ai.hermes.gateway"
GW_PLIST="$HOME/Library/LaunchAgents/${GW_LABEL}.plist"
SELF_LABEL="ai.hermes.self-restart"
STATUS="${1:-$HOME/.hermes/.gateway_restart_status}"
WLOG="$HOME/.hermes/logs/wake-debug.log"
RESUME_MARK="$HOME/.hermes/.post_restart_resume"
export PATH="$HOME/.hermes/hermes-agent/venv/bin:$PATH"
now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

printf '{"state":"restarting","at":"%s"}\n' "$(now)" > "$STATUS"

NOTE="$(cat "$HOME/.hermes/.restart_resume_note" 2>/dev/null || true)"
rm -f "$HOME/.hermes/.restart_resume_note"
# Capture what the agent was doing right NOW (before the gateway goes down), so
# it can be reminded on wake. Generic — pulls from agent.log + Colony timeline.
SUMMARY=""
if [ -f "$HOME/.hermes/scripts/pre-restart-summary.py" ]; then
  python3 "$HOME/.hermes/scripts/pre-restart-summary.py" >/dev/null 2>&1
  SUMMARY="$(cat "$HOME/.hermes/.post_restart_resume" 2>/dev/null || true)"
fi
WA="$(grep '^WHATSAPP_HOME_CHANNEL=' "$HOME/.hermes/.env" 2>/dev/null | cut -d= -f2-)"
MARC_DM="$(python3 -c "import json,os; j=json.load(open(os.path.expanduser('~/.hermes/cron/jobs.json'))); it=j if isinstance(j,list) else j.get('jobs',[]); o=[x.get('origin') or {} for x in it if x.get('id')=='efa5a0d53e43']; print((o[0].get('chat_id') if o else '') or '')" 2>/dev/null)"
[ -z "$MARC_DM" ] && MARC_DM="$WA"

launchctl bootout "gui/${UID_N}/${GW_LABEL}" 2>/dev/null
# Wait for the service to FULLY unload (port free AND not in launchctl list) —
# bootstrapping before the old instance fully exits races and silently fails.
for i in $(seq 1 30); do
  lsof -i :8644 -sTCP:LISTEN >/dev/null 2>&1 && { sleep 1; continue; }
  launchctl list 2>/dev/null | grep -q "${GW_LABEL}" && { sleep 1; continue; }
  break
done
BRIDGE_PIDS="$(lsof -ti :3000 2>/dev/null || true)"
[ -n "$BRIDGE_PIDS" ] && kill -9 $BRIDGE_PIDS 2>/dev/null
sleep 2

# Bring the gateway back up, retrying bootstrap+kickstart until :8644 listens
# (handles the unload/bootstrap race that previously left the gateway down).
for boot in 1 2 3; do
  launchctl bootstrap "gui/${UID_N}" "$GW_PLIST" 2>/dev/null
  launchctl kickstart "gui/${UID_N}/${GW_LABEL}" 2>/dev/null
  up=false
  for i in $(seq 1 12); do
    lsof -i :8644 -sTCP:LISTEN >/dev/null 2>&1 && { up=true; break; }
    sleep 1
  done
  $up && break
  sleep 2
done

# Now wait (up to ~180s) for the WhatsApp bridge to reconnect.
ok=false
for i in $(seq 1 36); do
  if lsof -i :8644 -sTCP:LISTEN >/dev/null 2>&1; then
    if curl -s -m 5 http://127.0.0.1:3000/health 2>/dev/null | grep -q '"status":"connected"'; then ok=true; break; fi
  fi
  sleep 5
done

if $ok; then
  printf '{"state":"ok","at":"%s","detail":"gateway restarted; bridge connected; wake sent"}\n' "$(now)" > "$STATUS"
  # Resume marker = manual note (if any) + the auto-captured pre-restart summary.
  # The 10-min worker cron reads this to resume; the wake message shows it so the
  # agent knows what it was doing right before the restart.
  { [ -n "$NOTE" ] && printf 'Note: %s\n\n' "$NOTE"; [ -n "$SUMMARY" ] && printf '%s\n' "$SUMMARY"; } > "$RESUME_MARK"
  WAKE_CTX="$(cat "$RESUME_MARK" 2>/dev/null || true)"
  if [ -n "$WAKE_CTX" ]; then
    MSG="$(printf '✅ Back online after a refresh (messaging + bridge healthy).\n\n⏮️ Right before the restart:\n%s\n\nIf you were mid-task, pick up where you left off.' "$WAKE_CTX")"
  else
    MSG="✅ Back online after a refresh. All channels healthy."
  fi
  # Restart notices go to the home/ops channel ($WA), NOT the owner's main chat
  # ($MARC_DM) — keeps the main Aeva conversation clean. Suppressed if no home
  # channel is configured (WHATSAPP_HOME_CHANNEL).
  { echo "--- wake $(now) | HOME=$WA | note_len=${#NOTE} ---"; } >> "$WLOG" 2>&1
  if [ -n "$WA" ]; then
    sleep 3   # let the bridge settle before sending
    hermes send -t "whatsapp:${WA}" "$MSG" >> "$WLOG" 2>&1
  fi
else
  printf '{"state":"failed","at":"%s","detail":"unhealthy after ~120s — recover: launchctl bootstrap"}\n' "$(now)" > "$STATUS"
fi

launchctl bootout "gui/${UID_N}/${SELF_LABEL}" 2>/dev/null
rm -f "$HOME/Library/LaunchAgents/${SELF_LABEL}.plist"
exit 0
