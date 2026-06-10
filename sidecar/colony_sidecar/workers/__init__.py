"""Schedulable agent-side workers (v0.20.0).

These are the cron-driven halves of the agent-as-sensor loop, packaged
so pip installs ship them as console scripts:

- ``colony-queue-worker``  (:mod:`colony_sidecar.workers.queue_worker`)
  claims approved ``agent_action`` jobs and hands them to the agent.
- ``colony-skills-sync``   (:mod:`colony_sidecar.workers.skills_sync`)
  reports the agent's installed skill index to Colony.

Both modules are deliberately stdlib-only: they must run from cron on
machines where only the agent (not the full sidecar dependency stack)
is present. Do not import heavy dependencies here.

The historical loose scripts under ``plugins/hermes-plugin/poller/``
remain as thin back-compat wrappers around these modules.
"""
