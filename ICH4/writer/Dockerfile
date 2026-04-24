FROM python:3.11-slim AS builder
WORKDIR /build
COPY ICH4/writer/requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

FROM python:3.11-slim
WORKDIR /app

COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

COPY ICH4/writer/api/        api/
COPY ICH4/writer/writer/     writer/
COPY ICH4/writer/validator/  validator/
COPY ICH4/writer/config/     config/
COPY system/bq_audit.py      bq_audit.py

ENV PYTHONPATH=/app
ENV PORT=8080

RUN useradd --create-home appuser
USER appuser

CMD ["sh", "-c", "uvicorn api.app:app --host 0.0.0.0 --port $PORT"]
