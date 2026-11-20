FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FXWATCH_CONFIG=/data/config.toml \
    TZ=Asia/Shanghai

WORKDIR /app
COPY requirements-docker.txt ./
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements-docker.txt \
    && mkdir -p /data /etc/fxwatch \
    && chown 10001:10001 /data

COPY fxwatch/ ./fxwatch/
COPY deploy/config.docker.toml /etc/fxwatch/default.toml

USER 10001:10001
EXPOSE 8099
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8099/api/summary', timeout=5).read()"]
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "fxwatch.container"]
