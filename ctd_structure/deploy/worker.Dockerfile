# ctd_structure/deploy/worker.Dockerfile
# Build context: ctd_structure/  (set by cloudbuild-worker.yaml dir: ctd_structure)
#
# Stage 1: install Python deps
FROM python:3.11-slim AS builder
WORKDIR /app
COPY deploy/worker_requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Stage 2: lean runtime image
FROM python:3.11-slim
WORKDIR /app

COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy ctd_structure package
COPY __init__.py   ctd_structure/__init__.py
COPY structure.py  ctd_structure/structure.py
COPY scaffold.py   ctd_structure/scaffold.py

# Copy worker entrypoint
COPY deploy/worker_app.py worker_app.py

ENV PYTHONPATH=/app

CMD ["sh", "-c", "python worker_app.py"]
