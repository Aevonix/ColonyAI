"""Schedulable agent-side workers, available as installed console commands.

- ``pacomind-agent-bridge`` (:mod:`pacomind.workers.agent_bridge`) runs
  initiative polling, job dispatch, skills sync and circuit health checks.
- ``pacomind-queue-worker`` (:mod:`pacomind.workers.queue_worker`) claims
  approved ``agent_action`` jobs and hands them to the agent.
- ``pacomind-skills-sync`` (:mod:`pacomind.workers.skills_sync`) reports
  the agent's installed skill index to PacoMind.

These modules are stdlib-only so the workers can run without the full
sidecar dependency stack. Do not import heavy dependencies here.
"""
