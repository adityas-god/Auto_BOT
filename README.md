# Headless Grafana to Slack Monitoring Bot & Web Operations Center

> A lightweight, 100% headless background service with a sleek Web Management UI that captures multiple Grafana dashboards/panels and uploads high-resolution snapshots directly to Slack channels.

---

## 🌟 Key Features

- **Web Operations UI (Port 5000)**: Access the dark-mode dashboard in your browser (`http://<SERVER_IP>:5000`) to manage monitoring links, configure credentials, test captures, and monitor live activity logs.
- **Multi-Dashboard Monitoring**: Monitor any number of Grafana dashboards or solo panels simultaneously. In each scheduled run, snapshots of all active links are taken and posted to Slack.
- **100% Headless**: Runs seamlessly in the background on any cloud Linux VM (Ubuntu/Debian) or Docker container without requiring a physical monitor, X11 desktop, or display server.
- **Zero Hardcoded Links**: All URLs, credentials, and settings are configured dynamically via the Web UI and synced directly to `.env` and `links.json`.
- **Modern Slack S3 Upload**: Uses Slack's official 3-step file upload API (`files.getUploadURLExternal` ➔ Direct S3 Upload ➔ `files.completeUploadExternal`).
- **Flexible Execution**:
  - Run continuous daemon + Web UI (`python main.py`)
  - Run single-shot capture for cron (`python main.py --once`)
  - Web UI only (`python main.py --ui-only`)
  - Daemon only (`python main.py --no-web`)

---

## 📁 Repository Structure

```
headless-bot/
├── .env                  # Active environment configuration (synced with UI)
├── .env.example          # Environment template
├── links.json            # Monitored Grafana links storage (managed via UI)
├── requirements.txt      # Python dependencies (playwright, requests, python-dotenv, flask, pytz)
├── config.py             # Configuration loader, validator, and .env synchronizer
├── grafana_capture.py    # Headless Playwright capture engine
├── slack_uploader.py     # Modern 3-step Slack API upload module
├── web_server.py         # Flask Web Management UI & REST API
├── main.py               # Main CLI and scheduler daemon entrypoint
├── templates/
│   └── index.html        # Modern dark-mode Web UI dashboard
├── setup.sh              # 1-step installer for Ubuntu Linux
├── Dockerfile            # Container definition exposing port 5000
├── docker-compose.yml    # Docker compose runner
└── README.md             # Documentation
```

---

## 🚀 Quick Start on Google Cloud (Ubuntu VM)

### 1. Run Setup Script
```bash
chmod +x setup.sh
./setup.sh
```

### 2. Start the Bot & Web UI
```bash
./venv/bin/python main.py
```

### 3. Open the Management UI
Open your browser and navigate to:
```
http://<YOUR_GCP_VM_EXTERNAL_IP>:5000
```
*(Ensure your GCP VPC firewall rule allows ingress on TCP port `5000`)*

From the Web UI, you can:
1. **Add your Grafana links** (e.g. `http://internal-ip:3000/d/...` or `https://grafana.domain.com/...`).
2. **Enter Slack credentials** (`SLACK_BOT_TOKEN`, `SLACK_CHANNEL_ID`).
3. Click **"Save Settings & Update .env"**.
4. Click **"Test Slack"** or **"Preview Capture"** on any link.
5. Click **"Run Capture Now"** to trigger an immediate snapshot cycle!

---

## 🛠️ Deploying as a 24/7 Linux Service (`systemd`)

To keep the bot and web UI running continuously in the background on your Ubuntu VM:

1. Create a service file:
   ```bash
   sudo nano /etc/systemd/system/grafana-slack-bot.service
   ```

2. Paste the following:
   ```ini
   [Unit]
   Description=Headless Grafana to Slack Monitoring Bot & Web UI
   After=network.target

   [Service]
   Type=simple
   User=ubuntu
   WorkingDirectory=/home/ubuntu/headless-bot
   ExecStart=/home/ubuntu/headless-bot/venv/bin/python main.py
   Restart=always
   RestartSec=10
   Environment=PYTHONUNBUFFERED=1

   [Install]
   WantedBy=multi-user.target
   ```

3. Enable and start:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now grafana-slack-bot
   ```

4. Check status & logs:
   ```bash
   sudo systemctl status grafana-slack-bot
   journalctl -u grafana-slack-bot -f
   ```

---

## 🧪 CLI Command Reference

| Command | Description |
| :--- | :--- |
| `python main.py` | Starts **both** the Web UI on port 5000 and the continuous background scheduler daemon. |
| `python main.py --once` | Executes capture & upload once for all active links and exits (ideal for cron). |
| `python main.py --ui-only` | Starts only the Web Management UI without running the scheduler loop. |
| `python main.py --no-web` | Starts only the scheduler loop without starting the web server. |
| `python main.py --test-slack` | Verifies Slack bot token and posts a test ping message to the configured channel. |
| `python main.py --test-grafana` | Takes headless screenshots of all active links and saves them locally. |
