# Official Playwright Python image (comes pre-installed with all Ubuntu libraries & fonts)
FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

WORKDIR /app

# Install Python requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright Chromium headless browser
RUN playwright install chromium

# Copy bot application
COPY main.py .

# Expose port for Web UI Management Dashboard
EXPOSE 5000

# Set unbuffered output for real-time Docker logs
ENV PYTHONUNBUFFERED=1

# Run the bot daemon and Web UI
CMD ["python", "-u", "main.py"]
