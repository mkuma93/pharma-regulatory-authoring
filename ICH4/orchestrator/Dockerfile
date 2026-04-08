FROM python:3.11-slim AS builder
WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

FROM python:3.11-slim
WORKDIR /app

COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

COPY main.py    main.py
COPY models.py  models.py
COPY config/    config/

ENV PYTHONPATH=/app
ENV PORT=8080

RUN useradd --create-home appuser
USER appuser

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port $PORT"]
