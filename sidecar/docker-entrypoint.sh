#!/bin/sh
set -eu
# Preserve an explicitly selected state directory.
if [ -z "${PACOMIND_STATE_DIR+x}" ]; then
    export PACOMIND_STATE_DIR=/var/lib/pacomind
fi
exec "$@"
