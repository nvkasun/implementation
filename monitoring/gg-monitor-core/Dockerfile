# syntax=docker/dockerfile:1
#
# =====================================================================
# MONITOR BASE IMAGE GOVERNANCE GATE -- DO NOT DISMISS.
# =====================================================================
# BASE_IMAGE has NO default and MUST be supplied by the packaging workflow
# as an approved, private-ECR, digest-pinned reference
# (repository@sha256:<digest>) -- matching the manager reference
# repository's own supply-chain pattern (private ECR mirror, digest
# pinning, boto3 baked in, no public runtime pip installation; see
# charts/gg-deployment/values.yaml image/sidecarImage blocks and
# templates/statefulset.yaml, inspected read-only, F:\claude\
# goldengate-managers-code, never modified or copied).
#
# This image is NOT deployable until an operator confirms and supplies:
#   1. An approved private ECR mirror of a Python base image that already
#      bundles this module's runtime dependencies (requirements.txt --
#      boto3/botocore/PyYAML/jmespath/python-dateutil/s3transfer/six/
#      urllib3), OR
#   2. An approved internal Python package repository URL, so a future
#      revision of this Dockerfile can install requirements.txt from THAT
#      source instead.
# Neither has been supplied yet. Guessing a public.ecr.aws mirror alias, a
# Docker Hub tag (e.g. the previous default python:3.12-slim), or an
# internal package repository URL here would repeat exactly the "do not
# invent an internal URL" mistake this gate exists to prevent.
#
# Enforcement (fails closed, does not silently fall back to a public
# source):
#   - `docker build` with no --build-arg BASE_IMAGE fails immediately below
#     (empty FROM reference).
#   - .github/workflows/gg-monitor-core.yaml's own governance-gate check
#     refuses to even invoke `docker build` without an approved value
#     configured.
#   - The dependency-presence RUN check further down fails the build
#     closed if BASE_IMAGE does not already provide every required
#     runtime dependency -- this Dockerfile never runs `pip install`
#     against a public index.
# =====================================================================
ARG BASE_IMAGE
FROM ${BASE_IMAGE}

ARG OCI_SOURCE=""
ARG OCI_TITLE="gg-monitor-core"
ARG OCI_DESCRIPTION="Shared, passive GoldenGate runtime poller/writer (LEASE/STATE/CloudWatch)"
ARG OCI_VERSION="development"

LABEL org.opencontainers.image.source="${OCI_SOURCE}" \
      org.opencontainers.image.title="${OCI_TITLE}" \
      org.opencontainers.image.description="${OCI_DESCRIPTION}" \
      org.opencontainers.image.version="${OCI_VERSION}"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

ARG APP_UID=10001
ARG APP_GID=10001
ARG APP_HOME=/app

RUN groupadd --system --gid "${APP_GID}" monitor \
    && useradd --system --uid "${APP_UID}" --gid "${APP_GID}" \
       --home-dir "${APP_HOME}" --no-create-home --shell /usr/sbin/nologin monitor \
    && mkdir -p "${APP_HOME}" \
    && chown "${APP_UID}:${APP_GID}" "${APP_HOME}"

WORKDIR ${APP_HOME}

# requirements.txt is retained as documentation of exactly what BASE_IMAGE
# (or a future approved internal package repository) must already provide
# -- it is NOT pip-installed here. No public PyPI installation (fix 4).
COPY requirements.txt ./
RUN python3 -c "import importlib.util as u, sys; missing=[m for m in ('boto3','botocore','yaml','jmespath','dateutil','s3transfer','six','urllib3') if u.find_spec(m) is None]; sys.exit(0) if not missing else sys.exit('MONITOR BASE IMAGE GOVERNANCE GATE: BASE_IMAGE is missing required runtime dependencies %r -- this build refuses to silently pip install from public PyPI; see the governance gate comment at the top of this Dockerfile.' % missing)"

COPY gg_monitor_core.py gg_health_rules.py inventory.py ./

USER ${APP_UID}:${APP_GID}

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
  CMD ["python3", "-c", "import os,urllib.request,sys; port=os.environ.get('PORT','8080'); sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/healthz' % port, timeout=2).status == 200 else 1)"]

ENTRYPOINT ["python3", "gg_monitor_core.py"]
