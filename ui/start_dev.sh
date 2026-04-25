#!/bin/sh
export CTD_API_URL="https://ctd-api-811317821863.us-central1.run.app"
export ICH4_WRITER_URL="https://ich4-writer-811317821863.us-central1.run.app"
export CLINICAL_ANALYST_URL="https://clinical-analyst-811317821863.us-central1.run.app"
export PYTHONUNBUFFERED=1
exec python -u ui/app_pro.py
