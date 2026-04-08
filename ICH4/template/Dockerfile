FROM python:3.11-slim AS builder
WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

FROM python:3.11-slim
WORKDIR /app

COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

COPY api/             api/
COPY template/        template/
COPY clinical/        clinical/
COPY ctd_structure/   ctd_structure/
COPY config/          config/

ENV PYTHONPATH=/app
ENV PORT=8080

RUN useradd --create-home appuser
USER appuser

CMD ["sh", "-c", "uvicorn api.app:app --host 0.0.0.0 --port $PORT"]
