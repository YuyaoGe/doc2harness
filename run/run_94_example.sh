#!/usr/bin/env bash
set -Eeuo pipefail

# Run the frozen 94-point catalog for two candidate artifacts.
# This deliberately uses the same YAML-driven runner as run_from_config.sh;
# it does not reuse recorded verdicts or manufacture a score from old receipts.

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="$(cd -- "${script_dir}/.." && pwd -P)"
config_path="${repo_root}/run/configs/example-94.yaml"

exec python3 "${script_dir}/run_config.py" \
  --repo-root "${repo_root}" \
  --config "${config_path}" \
  --require-models candidate-a,candidate-b \
  --require-count 94 \
  "$@"
