# No public default: BASE_IMAGE must be explicitly supplied as a
# digest-pinned, private-ECR reference by the caller (see
# .github/workflows/goldengate-monitor.yaml's "Validate approved base image
# reference" step). There is deliberately no Docker Hub fallback.
ARG BASE_IMAGE

FROM ${BASE_IMAGE}

ARG OCI_SOURCE=""
ARG OCI_TITLE="goldengate-monitor"
ARG OCI_DESCRIPTION="Shared GoldenGate collector and monitoring portal"
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

COPY requirements.txt ./
RUN pip install --no-cache-dir --no-compile -r requirements.txt

COPY monitor.py collector.py health_rules.py config.py ./
COPY tools/ ./tools/

USER ${APP_UID}:${APP_GID}

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
  CMD ["python3", "-c", "import os,urllib.request,sys; port=os.environ.get('PORT','8080'); sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/healthz' % port, timeout=2).status == 200 else 1)"]

ENTRYPOINT ["python3", "monitor.py"]
