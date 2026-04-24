# Clinical Analyst Service
FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY clinical-analyst/requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy service code
COPY clinical-analyst/app.py app.py

# PORT is injected by Cloud Run at runtime; 8080 is the default.
# Do NOT hardcode a fixed PORT here — Cloud Run picks the port.
EXPOSE 8080

CMD ["python", "app.py"]
