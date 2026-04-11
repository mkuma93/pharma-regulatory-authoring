# Clinical Analyst Service
FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY clinical-analyst/requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy service code
COPY clinical-analyst/app.py app.py

ENV PORT=8084

EXPOSE 8084

CMD ["python", "app.py"]
