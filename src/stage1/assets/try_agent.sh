#!/usr/bin/env bash
# Run a stage1-built product against a live model backend, either in the
# interactive TUI or as a one-shot headless prompt.
#
#   ./try_agent.sh --list                       # list products under results/stage1
#   ./try_agent.sh -a <run-id>                  # pick a product (run-id or directory)
#   ./try_agent.sh -a <run-id> --port 4000      # pick the backend gateway port
#   ./try_agent.sh -p "fix the bug in calc.py"  # one-shot headless (adds --mode yolo)
#   ./try_agent.sh -w ~/mycode -p "add a test"  # mount your own directory as the workspace
#   ./try_agent.sh --clean                      # remove the container
#
# Notes:
#   * The product runs in the same image used to build it, installing its
#     dependencies offline from the wheels it ships with. Products are built to
#     the contract "copy into a clean container and run".
#   * The backend is a LiteLLM gateway already running on the host, reached from
#     the container via --add-host. The gateway must expose one model alias named
#     `builder`. Set the port with --port or D2A_GATEWAY_PORT.
set -uo pipefail

# Repository root. This script lives in src/stage1/assets/, so go up three
# levels.
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
RESULTS="${D2A_RESULTS_DIR:-$REPO/results/stage1}"
IMAGE="${D2A_BUILDER_IMAGE:-ghcr.io/yuyaoge/doc2harness-builder:latest}"
CONTAINER="yuyao-try"
GW_KEY="${D2A_GATEWAY_KEY:-sk-d2a-local-master}"

AGENT_ARG=""        # run-id or product directory; empty means the newest build
PORT="${D2A_GATEWAY_PORT:-4000}"
PROMPT=""           # non-empty means one-shot headless
WORKDIR=""          # host directory to mount as the workspace

usage() { sed -n '2,18p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

list() {
  echo "Products in $RESULTS:"
  local found=0
  for d in "$RESULTS"/*/agent; do
    [ -f "$d/run_agent.sh" ] || continue
    found=1
    printf "  %-24s %s\n" "$(basename "$(dirname "$d")")" "$d"
  done
  [ "$found" -eq 1 ] || echo "  (none; run run_build.py, or point D2A_RESULTS_DIR elsewhere)"
  echo
  echo "Backend gateway: --port <port> (currently :$PORT); must expose the \`builder\` alias."
  ss -lntp 2>/dev/null | grep -q ":$PORT " \
    && echo "  :$PORT up" || echo "  :$PORT not running"
}

while [ $# -gt 0 ]; do
  case "$1" in
    -a|--agent)    AGENT_ARG="$2"; shift 2 ;;
    --port)        PORT="$2"; shift 2 ;;
    -p|--prompt)   PROMPT="$2"; shift 2 ;;
    -w|--workdir)  WORKDIR="$2"; shift 2 ;;
    --list)        list; exit 0 ;;
    --clean)       docker rm -f "$CONTAINER" >/dev/null 2>&1 && echo "removed container $CONTAINER"; exit 0 ;;
    -h|--help)     usage; exit 0 ;;
    *) echo "unknown argument $1 (see -h)"; exit 1 ;;
  esac
done

# Accept a product directory, a run directory, a run-id, or nothing (newest).
if [ -z "$AGENT_ARG" ]; then
  AGENT_DIR="$(ls -td "$RESULTS"/*/agent 2>/dev/null | head -1)"
elif [ -f "$AGENT_ARG/run_agent.sh" ]; then
  AGENT_DIR="$AGENT_ARG"
elif [ -f "$AGENT_ARG/agent/run_agent.sh" ]; then
  AGENT_DIR="$AGENT_ARG/agent"
elif [ "${AGENT_ARG#/}" != "$AGENT_ARG" ] || [ -d "$AGENT_ARG" ]; then
  # An absolute path, or an existing directory, was meant literally: do not treat it as a
  # run-id and paste it after $RESULTS, which produced paths like results/stage1//abs/path/agent.
  AGENT_DIR="$AGENT_ARG"
else
  AGENT_DIR="$RESULTS/$AGENT_ARG/agent"
fi

[ -n "${AGENT_DIR:-}" ] && [ -f "$AGENT_DIR/run_agent.sh" ] || {
  echo "no product found: '${AGENT_ARG:-<newest>}' -> '${AGENT_DIR:-}'"
  echo "  try --list, or -a <product dir>, or run run_build.py first"
  exit 1
}
AGENT_DIR="$(cd "$AGENT_DIR" && pwd)"

ss -lntp 2>/dev/null | grep -q ":$PORT " || {
  echo "no gateway on :$PORT. Start a LiteLLM gateway exposing the \`builder\` alias:"
  echo "    D2A_GATEWAY_PORT=$PORT D2A_STAGE1_CFG=<your litellm config> \\"
  echo "      bash src/stage1/gateway.example/start_gateway.sh"
  echo "  (the config holding real keys is not shipped; see src/stage1/README.md)"
  exit 1
}

echo "== product: $AGENT_DIR   backend: :$PORT =="

# Reuse a running container; mount the product read-only so it cannot be edited.
if docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  echo "-- reusing the running container (use --clean to switch products)"
else
  docker rm -f "$CONTAINER" >/dev/null 2>&1
  MOUNTS=(-v "$AGENT_DIR:/opt/agent")
  [ -n "$WORKDIR" ] && MOUNTS+=(-v "$(cd "$WORKDIR" && pwd):/work")
  docker run --rm -d --name "$CONTAINER" \
    --add-host=host.docker.internal:host-gateway \
    "${MOUNTS[@]}" -w /opt/agent "$IMAGE" tail -f /dev/null >/dev/null || exit 1
  echo "-- installing product dependencies offline from its bundled wheels"
  # install.sh ships without the executable bit, so invoke it through bash.
  docker exec "$CONTAINER" bash -lc "cd /opt/agent && bash install.sh" >/tmp/try_agent_install.log 2>&1 || {
    echo "install failed, see /tmp/try_agent_install.log"; tail -12 /tmp/try_agent_install.log; exit 1; }
  docker exec "$CONTAINER" bash -lc 'command -v yuyao >/dev/null && yuyao --version' \
    | sed 's/^/   /' || { echo "yuyao did not install"; exit 1; }
fi

ENVS=(-e "D2A_MODEL_BASE_URL=http://host.docker.internal:$PORT"
      -e "D2A_MODEL_NAME=litellm_proxy/builder"
      -e "D2A_MODEL_API_KEY=$GW_KEY")
CWD="/work"
[ -n "$WORKDIR" ] || { docker exec "$CONTAINER" bash -lc 'mkdir -p /work' >/dev/null; }

if [ -n "$PROMPT" ]; then
  # Headless needs --mode yolo, or the agent waits for a confirmation that never
  # comes and only offers suggestions instead of acting.
  echo "-- headless run (--mode yolo)"
  docker exec "${ENVS[@]}" "$CONTAINER" bash -lc \
    "cd $CWD && yuyao -p $(printf '%q' "$PROMPT") --mode yolo"
else
  echo "-- interactive TUI (workspace $CWD${WORKDIR:+ <- $WORKDIR}); container persists, use --clean"
  docker exec -it "${ENVS[@]}" "$CONTAINER" bash -lc "cd $CWD && yuyao"
fi
