#!/usr/bin/env bash
# ==============================================================================
# GreyOrange OpsBot — Linux VM Automated Setup Script (Ubuntu / Debian / GCP)
# ==============================================================================
set -e

echo "================================================================="
echo "🚀 Initializing GreyOrange OpsBot Environment on Linux VM"
echo "================================================================="

# 1. Update package lists and install system dependencies
echo "[1/4] Installing system packages & OCR dependencies..."
sudo apt-get update -y
sudo apt-get install -y \
    python3 \
    python3-pip \
    python3-venv \
    tesseract-ocr \
    tesseract-ocr-eng \
    curl \
    ca-certificates

# 2. Setup isolated Python virtual environment
echo "[2/4] Setting up Python virtual environment (venv)..."
if [ ! -d "venv" ]; then
    python3 -m venv venv
fi

# 3. Install Python requirements
echo "[3/4] Installing Python requirements..."
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

# 4. Install Playwright browser engine & system dependencies
echo "[4/4] Installing Playwright Chromium & browser libraries..."
./venv/bin/playwright install chromium
./venv/bin/playwright install-deps chromium || true

# 5. Create settings template if not present
if [ ! -f ".env" ] && [ -f ".env.example" ]; then
    echo "[INFO] Creating .env from .env.example..."
    cp .env.example .env
fi

echo "================================================================="
echo "✅ Setup Completed Successfully!"
echo "================================================================="
echo "To start OpsBot manually:"
echo "   ./venv/bin/python main.py"
echo ""
echo "To start as a background systemd service:"
echo "   sudo systemctl enable --now grafana-slack-bot"
echo "================================================================="
