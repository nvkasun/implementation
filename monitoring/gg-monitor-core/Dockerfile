# syntax=docker/dockerfile:1
#
# Build pattern (correction pass): reuses the same proven Docker build
# approach as the previous successful GoldenGate monitor implementation
# (monitoring/monitor/Dockerfile, .github/workflows/goldengate-monitor.yaml
# -- inspected read-only, application code/schema/architecture NOT copied)
# -- a public python:3.12-slim default base, pinned pip install from
# requirements.txt during the GitHub-hosted Docker build, non-root
# 10001:10001, and a private-ECR-pushed, content-addressed, immutable
# application image tag. The application image itself is still pushed only
# to the private ECR repository
# (229410149234.dkr.ecr.eu-west-1.amazonaws.com/gg-monitor-core) -- the
# public base is used only as the GitHub-hosted runner's OWN build input,
# never the final deployed artifact's registry.

ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}

ARG OCI_SOURCE=""
ARG OCI_TITLE="gg-monitor-core"
ARG OCI_DESCRIPTION="Shared, passive GoldenGate runtime poller and portal"
ARG OCI_VERSION="development"

LABEL org.opencontainers.image.source="${OCI_SOURCE}" \
      org.opencontainers.image.title="${OCI_TITLE}" \
      org.opencontainers.image.description="${OCI_DESCRIPTION}" \
      org.opencontainers.image.version="${OCI_VERSION}"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

ARG APP_UID=10001
ARG APP_GID=10001
ARG APP_HOME=/app

RUN groupadd --system --gid "${APP_GID}" monitor \
    && useradd --system \
       --uid "${APP_UID}" \
       --gid "${APP_GID}" \
       --home-dir "${APP_HOME}" \
       --no-create-home \
       --shell /usr/sbin/nologin \
       monitor \
    && mkdir -p "${APP_HOME}" \
    && chown "${APP_UID}:${APP_GID}" "${APP_HOME}"

WORKDIR ${APP_HOME}

COPY requirements.txt ./

RUN python3 -m pip install \
      --no-cache-dir \
      --no-compile \
      -r requirements.txt \
    && python3 -c "import boto3, botocore, yaml, jmespath, dateutil, s3transfer, six, urllib3; print('gg-monitor-core dependencies verified')"

COPY gg_monitor_core.py gg_health_rules.py inventory.py ./

USER ${APP_UID}:${APP_GID}

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
  CMD ["python3", "-c", "import os,urllib.request,sys; port=os.environ.get('PORT','8080'); sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/healthz' % port, timeout=2).status == 200 else 1)"]

ENTRYPOINT ["python3", "gg_monitor_core.py"]
