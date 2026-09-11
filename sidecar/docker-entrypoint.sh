#!/bin/sh
set -eu
# Existing mounts and either explicitly selected environment name win.
if [ -z "${APSIMO_STATE_DIR+x}" ] && [ -z "${COLONY_STATE_DIR+x}" ]; then
    export APSIMO_STATE_DIR=/var/lib/colony
fi
exec "$@"
