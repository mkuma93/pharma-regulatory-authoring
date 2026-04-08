# ICH4 content worker
# Build context: ICH4/content_worker/
FROM python:3.11-slim AS builder
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

FROM python:3.11-slim
WORKDIR /app
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY worker_app.py .
ENV PYTHONPATH=/app
CMD ["sh", "-c", "python worker_app.py"]
