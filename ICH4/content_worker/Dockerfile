# ICH4 content worker
# Build context: repo root (docker build -f ICH4/content_worker/Dockerfile .)
FROM python:3.11-slim AS builder
WORKDIR /app
COPY ICH4/content_worker/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

FROM python:3.11-slim
WORKDIR /app
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY ICH4/content_worker/worker_app.py .
COPY system/bq_audit.py                .
COPY system/iap_identity.py            .
ENV PYTHONPATH=/app
CMD ["sh", "-c", "python worker_app.py"]
