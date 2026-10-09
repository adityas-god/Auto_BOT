FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

WORKDIR /app

# Install system dependencies (Tesseract OCR for Grafana gauge reading & OCR extraction)
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

# Install Python requirements (Chromium is already pre-installed in the base image)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source code and packages
COPY main.py .
COPY core/ ./core/
COPY engines/ ./engines/
COPY scheduler/ ./scheduler/
COPY web/ ./web/
COPY sites.json* .
COPY grafana_auth_state.json* .
COPY .env.example .

# Expose Web UI management port
EXPOSE 5000

# Ensure logs stream in real-time
ENV PYTHONUNBUFFERED=1

# Start the bot daemon & Web UI
CMD ["python", "-u", "main.py"]
