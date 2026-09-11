#!/usr/bin/env bash
# Compatibility launcher for the supported, profile-aware Apsimo installer.
set -euo pipefail

args=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --memory) shift ;; # The guided attachment includes the memory provider.
    --force)
      echo "--force no longer bypasses attachment checks; guided setup retains existing state." >&2
      shift ;;
    *) args+=("$1"); shift ;;
  esac
done
exec "${APSIMO_PYTHON:-python3}" -m apsimo init \
  --hermes-home "${HERMES_HOME:-$HOME/.hermes}" "${args[@]}"
