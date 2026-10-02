FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

WORKDIR /app

# Install system packages including Tesseract OCR engine for Linux container
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-eng \
    libtesseract-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright Chromium headless browser
RUN playwright install chromium

# Copy application source code and template
COPY main.py .
COPY sites.json* .
COPY .env.example .

# Expose Web UI management port
EXPOSE 5000

# Ensure logs stream in real-time
ENV PYTHONUNBUFFERED=1

# Start the bot daemon & Web UI
CMD ["python", "-u", "main.py"]
