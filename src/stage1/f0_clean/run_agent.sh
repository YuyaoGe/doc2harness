#!/usr/bin/env bash
# Unattended entrypoint. Arguments and input file formats are specified in
# `.d2a_spec/AGENT_CONTRACT.md`.
set -uo pipefail
exec python3 "$(dirname "$0")/entry.py" "$@"
