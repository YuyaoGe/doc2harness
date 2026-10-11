#!/usr/bin/env bash
set -Eeuo pipefail

# Run the harness from a YAML run configuration.
# The Python entry point performs schema, catalog, and executor checks before
# starting any model process.  Paths in the YAML file are resolved relative to
# the repository unless they are explicitly absolute.

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="$(cd -- "${script_dir}/.." && pwd -P)"

if [[ $# -lt 1 ]]; then
  printf 'usage: %s CONFIG.yaml [--dry-run]\n' "$0" >&2
  exit 2
fi

config_path="$1"
shift
if [[ "${config_path}" != /* ]]; then
  config_path="${repo_root}/${config_path}"
fi

exec python3 "${script_dir}/run_config.py" \
  --repo-root "${repo_root}" \
  --config "${config_path}" \
  "$@"
