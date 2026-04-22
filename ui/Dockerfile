# Build context is the repo root.
# gcloud builds submit <repo_root> --config=ui/Dockerfile

FROM python:3.11-slim AS builder

WORKDIR /build
COPY ui/requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt


FROM python:3.11-slim

WORKDIR /app

COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Coordinator LangGraph pipeline
COPY ui/coordinator.py coordinator.py

# Gradio UI shell + routing
COPY ui/app.py app.py

ENV PYTHONPATH=/app
ENV PORT=8080

RUN useradd --create-home appuser
USER appuser

CMD ["sh", "-c", "python app.py"]
