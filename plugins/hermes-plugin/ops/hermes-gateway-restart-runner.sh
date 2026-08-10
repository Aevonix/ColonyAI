#!/usr/bin/env bash
# Inert compatibility target for the retired self-restart effect runner.

LEGACY_EFFECT_WORKER_DISABLED=1
export LEGACY_EFFECT_WORKER_DISABLED
echo "hermes-gateway-restart-runner: disabled; use an operator-approved deployment action" >&2
exit 78
