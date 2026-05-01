# Build context is the repo root.
# gcloud builds submit <repo_root> --config=ui/cloudbuild_pro.yaml

FROM python:3.11-slim AS builder

WORKDIR /build
COPY ui/requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt


FROM python:3.11-slim

WORKDIR /app

COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Coordinator LangGraph pipeline (shared with original UI)
COPY ui/coordinator.py coordinator.py

# Professional guided UI shell
COPY ui/app_pro.py app.py

ENV PYTHONPATH=/app
ENV PORT=8080
ENV PYTHONUNBUFFERED=1

RUN useradd --create-home appuser
USER appuser

CMD ["python", "app.py"]
