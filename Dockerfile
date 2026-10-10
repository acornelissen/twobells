# The departures dashboard and its settings (`departureboard serve --install
# docker`) for a NAS or any Docker host. Build and run it with
# docker-compose.yml; see docs/user-guide.md. It runs as uid 10001, the
# owner of the data volume since the first image, whatever its name.
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/src

# Dependencies from exact, hashed wheels only; then a user to run as.
COPY deploy/docker/requirements-docker.txt /tmp/requirements.txt
RUN pip install --require-hashes --only-binary=:all: --no-deps -r /tmp/requirements.txt \
 && rm /tmp/requirements.txt \
 && python -c "import zoneinfo; zoneinfo.ZoneInfo('Europe/London')" \
 && useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin departureboard \
 && mkdir -p /data /config \
 && chown 10001:10001 /data \
 && chmod 700 /data

# The code runs from source: nothing to build, nothing more to download.
# pyproject.toml beside src/ is where `departureboard --version` (and the
# dashboard and settings) read the version, as nothing is installed.
COPY pyproject.toml /app/pyproject.toml
COPY src /app/src
RUN python -c "import departureboard.cli.main; from departureboard.version import package_version as v; \
assert v() != 'unknown', 'no version in /app/pyproject.toml'"

USER 10001:10001
WORKDIR /data
EXPOSE 8090
HEALTHCHECK --interval=60s --timeout=5s --start-period=120s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/healthz', timeout=4)"]
ENTRYPOINT ["python", "-m", "departureboard", "serve", "--install", "docker", \
            "--config", "/config/config.toml", "--secrets", "/nonexistent", \
            "--state-dir", "/data", "--host", "0.0.0.0", "--port", "8090"]
