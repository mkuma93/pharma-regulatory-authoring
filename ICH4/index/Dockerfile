# Multi-stage — keeps the final image lean for faster Cloud Run cold starts

# ── Stage 1: install dependencies ─────────────────────────────────────────────
FROM python:3.11-slim AS builder
WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt


# ── Stage 2: runtime ──────────────────────────────────────────────────────────
FROM python:3.11-slim
WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application code
COPY api/             api/
COPY knowledge/       knowledge/
COPY config/          config/

ENV PYTHONPATH=/app

# Cloud Run injects PORT at runtime
ENV PORT=8080

# Index must live in /tmp (only writable path on Cloud Run)
ENV INDEX_PERSIST_DIR=/tmp/index_store

# Run as non-root
RUN useradd --create-home appuser
USER appuser

CMD ["sh", "-c", "uvicorn api.app:app --host 0.0.0.0 --port $PORT"]
