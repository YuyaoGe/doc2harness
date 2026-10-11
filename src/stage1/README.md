# stage1 — the product build pipeline

stage1 has a model build a harness product through OpenCode, following a
functional specification. stage2 (`doc2harness`) then scores that product. The
two are independent: **scoring an existing product needs only stage2**; you need
stage1 only to build a new one.

Two runtime inputs are **not** distributed with this repository. Supply them
yourself:

| Input | What it is | How to supply it |
|---|---|---|
| Builder image | Self-contained: the OpenCode binary plus the hollowed product skeleton at `/opt/f0`. Needs no network at run time. | `docker pull ghcr.io/yuyaoge/doc2harness-builder:latest` (the default). To build it yourself: `docker build -f src/stage1/assets/Dockerfile -t ghcr.io/yuyaoge/doc2harness-builder:latest src/stage1` (that tag is the default; any other needs `--image` or `D2A_BUILDER_IMAGE`). |
| Model gateway | A local OpenAI-compatible gateway holding **your** API key. | Copy the templates in [`gateway.example/`](gateway.example/), fill in your endpoint and key, and point `D2A_STAGE1_CFG` at your copy. See below. |

Contract names are fixed and must not be renamed — `D2A_*`, `yuyao`,
`.d2a_spec`, `run_agent.sh`, `entry.py`, `mini`. Renaming them breaks products.

## Setting up the gateway

The pipeline is deliberately model-agnostic: OpenCode sees exactly one model
alias, named `builder`, so the model being evaluated never learns which upstream
it is talking to. Any OpenAI-compatible endpoint works.

```bash
cp -r src/stage1/gateway.example /your/private/path/gateway
$EDITOR /your/private/path/gateway/litellm.config.yaml   # your endpoint + key
export D2A_STAGE1_CFG=/your/private/path/gateway/litellm.config.yaml
export D2A_GATEWAY_KEY=sk-your-local-gateway-key
bash /your/private/path/gateway/start_gateway.sh
```

Two things must line up, and `run_build` fails closed if they do not: the
gateway port (`--gateway-port`, default 4000, or `D2A_GATEWAY_PORT`) and the
`baseURL` in `src/stage1/opencode_profile/opencode.json`.

`D2A_GATEWAY_KEY` is a local credential that the build container presents to
your gateway. Your real upstream key stays on the host, in the gateway config,
and never enters the container.

## A. Score an existing product (stage2 only)

A product is a directory containing `src/yuyao/` — typically `<run>/agent`.

```bash
# full catalog (94 scenarios; omit --ids-file to select all of them)
PYTHONPATH=src:tools:run python3 tools/run_production_catalog.py \
  --artifact <product dir> --artifact-id <name> \
  --output <output dir> --concurrency 12 --timeout 300

# a subset: --family permission  or  --scenario-id <id>
```

Each per-scenario JSON carries `events[*].judgement` with `outcome` and
`applicability`. The score is applicability-first pass-over-applicable:
`pass / (pass + fail)`.

> Evaluation containers clean themselves up on a normal exit, but a run that is
> killed or interrupted leaves orphans behind. For long parallel runs, keep a
> janitor that periodically removes `ancestor=doc2harness/evaluation:candidate`
> containers older than the scenario timeout, and sweep once at the end.

## B. Build a new product, then score it

```bash
# 1) dry run first: prints the full orchestration plan, starts no container,
#    calls no model, costs nothing, and needs no gateway or key
PYTHONPATH=src python3 src/stage1/run_build.py --builder-tag <name> --dry-run

# 2) the real build (needs the image and a running gateway)
PYTHONPATH=src python3 src/stage1/run_build.py --builder-tag <name> \
  --out-dir <results dir>

# 3) score <results dir>/<run_id>/agent with flow A above
```

A build takes roughly 15 minutes, depending on the model.

> The builder image already bakes the hollowed skeleton into `/opt/f0` —
> contract sockets only — so you do not need `--f0-src`. Pass `--f0-src` only to
> substitute a different base.

## Dependencies

None. stage1 is standard library only, same as the evaluation core — building a
product needs Python 3.11+ and Docker and nothing else.
