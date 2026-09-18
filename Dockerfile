# ==============================================================================
# STAGE 1: Builder (Dependencies & Package Compilation)
# ==============================================================================
FROM python:3.11-slim AS builder

WORKDIR /build

# Install dependencies into /root/.local (isolated from system python)
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt


# ==============================================================================
# STAGE 2: Runtime (Minimal Execution Environment)
# ==============================================================================
FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy AS runtime

WORKDIR /app

# Copy only the compiled Python packages from the builder stage
COPY --from=builder /root/.local /root/.local
ENV PATH=/root/.local/bin:$PATH

# Install headless Chromium browser binaries for Playwright
RUN playwright install chromium

# Copy application source code
COPY main.py .

# Expose Web UI management port
EXPOSE 5000

# Ensure logs stream in real-time
ENV PYTHONUNBUFFERED=1

# Start the bot daemon & Web UI
CMD ["python", "-u", "main.py"]
