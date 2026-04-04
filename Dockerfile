FROM python:3.12-slim

WORKDIR /app

# bluez + dbus needed for BLE via bleak inside Docker
RUN apt-get update && apt-get install -y --no-install-recommends \
        bluez \
        dbus \
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
