FROM python:3.12-slim AS base

WORKDIR /app

# bluez + dbus needed for BLE via bleak; libusb for ANT+ via openant
RUN apt-get update && apt-get install -y --no-install-recommends \
        bluez \
        dbus \
        libusb-1.0-0 \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY pyproject.toml README.md ./
COPY src ./src
COPY workouts ./workouts
COPY tests ./tests

RUN pip install --no-cache-dir --upgrade pip setuptools wheel hatchling && \
    pip install --no-cache-dir ".[test]"

# ── test stage: runs pytest; fails build if any test fails ────────────────────

FROM base AS test

RUN pytest tests/ -v --tb=short

# ── runtime stage: lean image without test deps ───────────────────────────────

FROM python:3.12-slim AS runtime

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        bluez \
        dbus \
        libusb-1.0-0 \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=38097 \
    MCP_TRANSPORT=http \
    MCP_HOST=0.0.0.0 \
    MCP_PATH=/mcp

COPY pyproject.toml README.md ./
COPY src ./src
COPY workouts ./workouts

RUN pip install --no-cache-dir --upgrade pip setuptools wheel hatchling && \
    pip install --no-cache-dir .

ENV TREADMILL_WORKOUTS_DIR=/app/workouts

EXPOSE 38097

CMD ["fitness-machine-mcp"]
