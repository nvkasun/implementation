# syntax=docker/dockerfile:1
ARG BASE_IMAGE=python:3.12-slim

FROM ${BASE_IMAGE}

ARG OCI_SOURCE=""
ARG OCI_TITLE="goldengate-observer"
ARG OCI_DESCRIPTION="Fail-open GoldenGate monitoring observer sidecar"
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

RUN groupadd --system --gid "${APP_GID}" observer \
    && useradd --system --uid "${APP_UID}" --gid "${APP_GID}" \
       --home-dir "${APP_HOME}" --no-create-home --shell /usr/sbin/nologin observer \
    && mkdir -p "${APP_HOME}" \
    && chown "${APP_UID}:${APP_GID}" "${APP_HOME}"

WORKDIR ${APP_HOME}

COPY requirements.txt ./
RUN pip install --no-cache-dir --no-compile -r requirements.txt

COPY observer.py ./

USER ${APP_UID}:${APP_GID}

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
  CMD ["python3", "-c", "import os,urllib.request,sys; port=os.environ.get('HEALTH_LISTEN_PORT','8080'); sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/healthz' % port, timeout=2).status == 200 else 1)"]

ENTRYPOINT ["python3", "observer.py"]
