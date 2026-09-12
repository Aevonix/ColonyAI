"""Schedulable agent-side workers, available as installed console commands.

- ``apsimo-agent-bridge`` (:mod:`apsimo.workers.agent_bridge`) runs
  initiative polling, job dispatch, skills sync and circuit health checks.
- ``apsimo-queue-worker`` (:mod:`apsimo.workers.queue_worker`) claims
  approved ``agent_action`` jobs and hands them to the agent.
- ``apsimo-skills-sync`` (:mod:`apsimo.workers.skills_sync`) reports
  the agent's installed skill index to Apsimo.

These modules are stdlib-only so the workers can run without the full
sidecar dependency stack. Do not import heavy dependencies here.
"""
