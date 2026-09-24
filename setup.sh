#!/usr/bin/env bash
# ==============================================================================
# Headless Grafana to Slack Monitoring Bot - 1-Step Setup Script for Ubuntu Linux
# ==============================================================================
set -e

echo "=========================================================="
echo "🚀 Setting up Headless Grafana to Slack Monitoring Bot"
echo "=========================================================="

# 1. Update package list and install system prerequisites
echo "📦 Installing system dependencies..."
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip curl ca-certificates

# 2. Set up Python virtual environment
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [ ! -d "venv" ]; then
    echo "🐍 Creating Python virtual environment (venv)..."
    python3 -m venv venv
else
    echo "🐍 Virtual environment already exists."
fi

# 3. Upgrade pip and install Python packages
echo "📥 Installing Python requirements..."
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

# 4. Install Playwright Chromium browser and its system OS dependencies
echo "🌐 Installing Playwright Chromium browser & OS libraries..."
./venv/bin/playwright install --with-deps chromium

# 5. Initialize .env from .env.example if missing
if [ ! -f ".env" ] && [ -f ".env.example" ]; then
    echo "⚙️ Initializing .env configuration from .env.example..."
    cp .env.example .env
fi

echo "=========================================================="
echo "✅ Setup successfully completed!"
echo ""
echo "To start the Bot & Web Management UI:"
echo "   ./venv/bin/python main.py"
echo ""
echo "Open the Web Operations Center at:"
echo "   http://<YOUR_SERVER_IP>:5000"
echo "=========================================================="
