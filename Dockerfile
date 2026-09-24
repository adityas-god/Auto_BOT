FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

WORKDIR /app

# Install Python requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright Chromium headless browser
RUN playwright install chromium

# Copy application source code and template
COPY main.py .
COPY .env.example .

# Expose Web UI management port
EXPOSE 5000

# Ensure logs stream in real-time
ENV PYTHONUNBUFFERED=1

# Start the bot daemon & Web UI
CMD ["python", "-u", "main.py"]
