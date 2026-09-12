#!/usr/bin/env bash
# Compatibility launcher for the supported, profile-aware PacoMind installer.
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
exec "${PACOMIND_PYTHON:-python3}" -m pacomind init \
  --hermes-home "${HERMES_HOME:-$HOME/.hermes}" "${args[@]}"
