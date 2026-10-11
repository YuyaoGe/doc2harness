# syntax=docker/dockerfile:1.7

ARG PYTHON_IMAGE=python:3.12-slim@sha256:090ba77e2958f6af52a5341f788b50b032dd4ca28377d2893dcf1ecbdfdfe203

FROM ${PYTHON_IMAGE} AS runtime
ARG DEBIAN_MIRROR=https://mirrors.aliyun.com/debian
ARG DEBIAN_SECURITY_MIRROR=https://mirrors.aliyun.com/debian-security
ARG PYPI_INDEX_URL=https://mirrors.aliyun.com/pypi/simple
ARG SOURCE_DATE_EPOCH=315532800
ENV DEBIAN_FRONTEND=noninteractive \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1
RUN sed -i \
      -e "s|http://deb.debian.org/debian-security|${DEBIAN_SECURITY_MIRROR}|g" \
      -e "s|http://deb.debian.org/debian|${DEBIAN_MIRROR}|g" \
      /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
      bash ca-certificates curl dnsutils git iproute2 iptables ripgrep unzip \
    && rm -rf /var/lib/apt/lists/* /var/log/apt/* \
    && rm -f /var/log/alternatives.log /var/log/dpkg.log \
      /var/cache/ldconfig/aux-cache
COPY containers/requirements/runtime.lock /tmp/runtime.lock
RUN python -m pip install --no-cache-dir --index-url "${PYPI_INDEX_URL}" \
      --requirement /tmp/runtime.lock \
    && python -m pip check \
    && find /usr/local/lib/python3.12/site-packages \
      -type f -name '*.pyc' -delete \
    && PYTHONHASHSEED=0 SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH}" \
      python -m compileall -q -f --invalidation-mode checked-hash \
      /usr/local/lib/python3.12/site-packages \
    && python - <<'PY'
import importlib.metadata
import importlib.util

assert importlib.util.find_spec("minisweagent") is None
try:
    importlib.metadata.distribution("mini-swe-agent")
except importlib.metadata.PackageNotFoundError:
    pass
else:
    raise SystemExit("mini-swe-agent distribution leaked into runtime image")
PY
RUN mkdir -p /opt/f0 && test -z "$(find /opt/f0 -mindepth 1 -print -quit)"
WORKDIR /opt/f0

FROM runtime AS evaluation
LABEL org.opencontainers.image.title="doc2harness evaluation environment" \
      org.opencontainers.image.description="Minimal environment for isolated artifact evaluation"
RUN test ! -e /usr/local/bin/opencode \
    && test ! -e /root/.opencode \
    && test -z "$(find /opt/f0 -mindepth 1 -print -quit)"
