# Operations boundary

The governed general plugin does not install anything from this directory.
These files are retained for migration and diagnostics only.

- `colony-doctor.py` performs static/read-only integration checks and reports a
  non-zero exit status on drift.
- `colony-doctor-cron.sh` writes the doctor result to a local log. It does not
  send a message.
- `hermes-patch-runner.py` inventories old patch files without executing them.
  A clean deployment has no Hermes core patch registry.
- `colony-activity-monitor.py` and `hermes-gateway-restart-runner.sh` are inert
  compatibility targets. Their former direct notification/restart behavior
  bypassed the action plane.
- `pre-restart-summary.py` reads the local agent log and Colony timeline, then
  writes `~/.hermes/.post_restart_resume`; it is a local-state-writing summary
  helper and is not installed by the plugin.

Effects such as outbound notices, service restarts, or source changes must be
requested through the deployment's authenticated action plane. Existing jobs
that invoke an inert compatibility target should be removed after upgrade.
