FROM python:3.13-slim AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
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
CMD ["big", "run"]
