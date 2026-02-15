# ── Stage 1: dependency builder ────────────────────────────────────────────────
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /build

# Install build-time system deps required by some Python packages (e.g. bleak/dbus)
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libdbus-1-dev \
    && rm -rf /var/lib/apt/lists/*

# Create an isolated venv so only it needs to be copied to the runtime stage
RUN python -m venv /venv
ENV PATH="/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt


# ── Stage 2: minimal runtime image ─────────────────────────────────────────────
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/venv/bin:$PATH"

# Runtime-only system libs (dbus needed by bleak at runtime)
RUN apt-get update && apt-get install -y --no-install-recommends \
        libdbus-1-3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy venv from builder (no pip / compiler in final image)
COPY --from=builder /venv /venv

# Copy application source
COPY app/ ./app/
COPY sql/ ./sql/

# Bake the .env into the image (production config is part of the build)
COPY .env ./.env

# Non-root user for security
RUN adduser --disabled-password --gecos "" appuser \
 && chown -R appuser /app
USER appuser

EXPOSE 8080

# Single worker: the MeshCore device accepts only one connection at a time.
# Multiple workers would race on the same TCP/BLE/Serial endpoint with no
# cross-process coordination, causing "TCPTransport closed" errors.
# FastAPI/asyncio handles concurrent HTTP and WebSocket clients within one
# worker without any throughput loss for this workload.
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8080", \
     "--workers", "1", \
     "--no-access-log", \
     "--no-use-colors"]
