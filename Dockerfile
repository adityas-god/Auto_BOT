FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

WORKDIR /app

# Install Python requirements (Chromium is already pre-installed in the base image)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

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
