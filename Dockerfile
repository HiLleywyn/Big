FROM python:3.13-slim AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY config.example.yaml ./
COPY src ./src
RUN python -m pip wheel --wheel-dir /wheels .

FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    BIG_DATABASE_PATH=/app/data/big.db

RUN groupadd --system big && useradd --system --gid big --home-dir /app big
WORKDIR /app
COPY --from=build /wheels /wheels
RUN python -m pip install --no-cache-dir /wheels/* && rm -rf /wheels \
    && mkdir -p /app/data && chown -R big:big /app
USER big

VOLUME ["/app/data"]
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/healthz', timeout=3)"
CMD ["big", "run"]
