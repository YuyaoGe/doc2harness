#!/usr/bin/env bash
# Start the local model gateway for a stage1 build.
#
# This is a template. It carries no credentials: the upstream key lives in your
# own litellm config (see litellm.config.yaml in this directory) and never
# enters the build container, which only ever sees D2A_GATEWAY_KEY.
#
#   export D2A_STAGE1_CFG=/your/private/path/litellm.config.yaml
#   export D2A_GATEWAY_KEY=sk-your-local-gateway-key
#   export YOUR_UPSTREAM_API_KEY=...          # referenced by your config
#   bash src/stage1/gateway.example/start_gateway.sh
#
# Then build against it:
#   PYTHONPATH=src python3 src/stage1/run_build.py --builder-tag <name>
set -Eeuo pipefail

PORT="${D2A_GATEWAY_PORT:-4000}"
CONFIG="${D2A_STAGE1_CFG:-}"

if [ -z "$CONFIG" ] || [ ! -f "$CONFIG" ]; then
  echo "[gateway] set D2A_STAGE1_CFG to your litellm config" >&2
  echo "          template: src/stage1/gateway.example/litellm.config.yaml" >&2
  exit 2
fi
if [ -z "${D2A_GATEWAY_KEY:-}" ]; then
  echo "[gateway] set D2A_GATEWAY_KEY (the local key the container will present)" >&2
  exit 2
fi
if ! python3 -c "import litellm.proxy" 2>/dev/null; then
  echo "[gateway] missing dependency: pip install 'litellm[proxy]'" >&2
  exit 2
fi

echo "[gateway] starting on :$PORT with $(basename "$CONFIG")"
# --host 0.0.0.0 so the build container can reach it via host.docker.internal.
exec litellm --config "$CONFIG" --port "$PORT" --host 0.0.0.0
