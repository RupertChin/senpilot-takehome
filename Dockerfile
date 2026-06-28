# Playwright base image ships Chromium + all system deps (spec §10).
FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy

WORKDIR /app

# Install deps first (layer-cached), then fetch the Chromium build matching the pinned Playwright.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && playwright install chromium

COPY app/ ./app/

# Cloud Run injects PORT (and K_SERVICE, which auto-selects ENV=prod).
ENV PORT=8080
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
