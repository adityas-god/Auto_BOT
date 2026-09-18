#!/usr/bin/env python3
"""
Grafana to Slack Headless Bot - Clean Streamlined Operations Hub
- 100% Headless browser capture (Playwright Chromium)
- Modern 3-step Slack S3 Upload API
- Ultra-Clean Minimal Single-Card Web UI with In-Browser Snapshot Preview
- Auto-syncs directly to .env
"""

import os
import sys
import time
import json
import tempfile
import threading
import argparse
from collections import deque
from datetime import datetime
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

import pytz
import requests
from dotenv import load_dotenv
from flask import Flask, render_template_string, request, jsonify, send_file
from playwright.sync_api import sync_playwright

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE_DIR, ".env")

if not os.path.exists(ENV_PATH):
    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.write("# Headless Bot Configuration\n")

load_dotenv(dotenv_path=ENV_PATH, override=True)


# ==============================================================================
# CONFIGURATION & .ENV MANAGER
# ==============================================================================
class Config:
    GRAFANA_URL = os.getenv("GRAFANA_URL", "").strip()
    GRAFANA_API_TOKEN = os.getenv("GRAFANA_API_TOKEN", "").strip()
    GRAFANA_COOKIE = os.getenv("GRAFANA_COOKIE", "").strip()
    GRAFANA_USERNAME = os.getenv("GRAFANA_USERNAME", "").strip()
    GRAFANA_PASSWORD = os.getenv("GRAFANA_PASSWORD", "").strip()
    SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "").strip()
    SLACK_CHANNEL_ID = os.getenv("SLACK_CHANNEL_ID", "").strip()
    SLACK_MESSAGE = os.getenv("SLACK_MESSAGE", "📊 *Grafana Snapshot Alert* - {datetime}").strip()
    SCHEDULE_INTERVAL_MINUTES = int(os.getenv("SCHEDULE_INTERVAL_MINUTES", "30"))
    BOT_PAUSED = os.getenv("BOT_PAUSED", "false").strip().lower() == "true"

    TIMEZONE = os.getenv("TIMEZONE", "Asia/Kolkata").strip()
    VIEWPORT_WIDTH = 1920
    VIEWPORT_HEIGHT = 1080
    PAGE_LOAD_WAIT_SECONDS = 8
    WEB_HOST = "0.0.0.0"
    WEB_PORT = int(os.getenv("WEB_PORT", "5000"))

    @classmethod
    def reload(cls):
        load_dotenv(dotenv_path=ENV_PATH, override=True)
        cls.GRAFANA_URL = os.getenv("GRAFANA_URL", "").strip()
        cls.GRAFANA_API_TOKEN = os.getenv("GRAFANA_API_TOKEN", "").strip()
        cls.GRAFANA_COOKIE = os.getenv("GRAFANA_COOKIE", "").strip()
        cls.GRAFANA_USERNAME = os.getenv("GRAFANA_USERNAME", "").strip()
        cls.GRAFANA_PASSWORD = os.getenv("GRAFANA_PASSWORD", "").strip()
        cls.SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "").strip()
        cls.SLACK_CHANNEL_ID = os.getenv("SLACK_CHANNEL_ID", "").strip()
        cls.SLACK_MESSAGE = os.getenv("SLACK_MESSAGE", "📊 *Grafana Snapshot Alert* - {datetime}").strip()
        cls.SCHEDULE_INTERVAL_MINUTES = int(os.getenv("SCHEDULE_INTERVAL_MINUTES", "30"))
        cls.BOT_PAUSED = os.getenv("BOT_PAUSED", "false").strip().lower() == "true"
        cls.TIMEZONE = os.getenv("TIMEZONE", "Asia/Kolkata").strip()
        cls.WEB_PORT = int(os.getenv("WEB_PORT", "5000"))

    @classmethod
    def parse_channel_id(cls, raw_input):
        val = raw_input.strip()
        if "/archives/" in val:
            parts = val.rstrip("/").split("/")
            return parts[-1]
        return val

    @classmethod
    def save_settings(cls, settings_dict):
        lines = []
        if os.path.exists(ENV_PATH):
            with open(ENV_PATH, "r", encoding="utf-8") as f:
                lines = f.readlines()

        updated = set()
        new_lines = []
        for line in lines:
            matched = False
            for k, v in settings_dict.items():
                if line.strip().startswith(f"{k}=") or line.strip().startswith(f"{k} ="):
                    new_lines.append(f"{k}={v}\n")
                    updated.add(k)
                    matched = True
                    break
            if not matched:
                new_lines.append(line)

        for k, v in settings_dict.items():
            if k not in updated:
                new_lines.append(f"{k}={v}\n")

        with open(ENV_PATH, "w", encoding="utf-8") as f:
            f.writelines(new_lines)

        cls.reload()
        return True

    @classmethod
    def get_formatted_message(cls):
        try:
            tz = pytz.timezone(cls.TIMEZONE)
            now = datetime.now(tz)
        except Exception:
            now = datetime.now()

        formatted_datetime = now.strftime("%Y-%m-%d %I:%M:%S %p")
        formatted_date = now.strftime("%Y-%m-%d")
        formatted_time = now.strftime("%I:%M:%S %p")

        msg = cls.SLACK_MESSAGE
        msg = msg.replace("{datetime}", formatted_datetime)
        msg = msg.replace("{date}", formatted_date)
        msg = msg.replace("{time}", formatted_time)
        msg = msg.replace("{grafana_url}", cls.GRAFANA_URL)
        return msg


# ==============================================================================
# HEADLESS CAPTURE ENGINE
# ==============================================================================
class GrafanaCapture:
    def __init__(self, config=None):
        self.cfg = config or Config

    def _prepare_url(self, raw_url):
        if not raw_url:
            return ""
        url = raw_url.strip()
        # Non-destructively append kiosk & theme without re-encoding existing variables ($__all, etc.)
        if "/d-solo/" not in url and "kiosk" not in url:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}kiosk=tv"
        if "theme=" not in url:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}theme=dark"
        return url

    def capture_screenshot(self, target_url=None, output_path=None, username=None, password=None, token=None, cookie=None):
        url_to_capture = target_url or self.cfg.GRAFANA_URL
        if not url_to_capture:
            raise ValueError("No Grafana URL configured.")

        if not output_path:
            tmp_dir = tempfile.gettempdir()
            filename = f"grafana_snapshot_{int(time.time() * 1000)}.png"
            output_path = os.path.join(tmp_dir, filename)

        prepared_url = self._prepare_url(url_to_capture)
        bot_log(f"🌐 Navigating headlessly to Grafana: {prepared_url}")

        user = username if username is not None else self.cfg.GRAFANA_USERNAME
        pwd = password if password is not None else self.cfg.GRAFANA_PASSWORD
        auth_token = token if token is not None else self.cfg.GRAFANA_API_TOKEN
        auth_cookie = cookie if cookie is not None else self.cfg.GRAFANA_COOKIE

        extra_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        if auth_token:
            extra_headers["Authorization"] = f"Bearer {auth_token}"
            bot_log("🔐 Authenticating with Grafana Service Account Token (Bearer)")

        if auth_cookie:
            extra_headers["Cookie"] = auth_cookie.strip()
            bot_log("🍪 Authenticating with Session Cookie")

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--hide-scrollbars",
                    "--mute-audio",
                    "--ignore-certificate-errors",
                    "--disable-web-security",
                    "--disable-blink-features=AutomationControlled"
                ]
            )

            context = browser.new_context(
                viewport={
                    "width": self.cfg.VIEWPORT_WIDTH,
                    "height": self.cfg.VIEWPORT_HEIGHT
                },
                device_scale_factor=1.0,
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                extra_http_headers=extra_headers,
                ignore_https_errors=True
            )

            # Inject cookies into context for client-side JS & subsequent requests
            if auth_cookie:
                try:
                    cookies_to_add = []
                    raw_cookie = auth_cookie.strip()
                    if raw_cookie.startswith("[") or raw_cookie.startswith("{"):
                        try:
                            parsed_json = json.loads(raw_cookie)
                            if isinstance(parsed_json, list):
                                for c in parsed_json:
                                    if isinstance(c, dict) and "name" in c and "value" in c:
                                        entry = {"name": str(c["name"]), "value": str(c["value"])}
                                        if "domain" in c:
                                            entry["domain"] = c["domain"]
                                        elif "url" in c:
                                            entry["url"] = c["url"]
                                        else:
                                            entry["url"] = prepared_url
                                        if "path" in c:
                                            entry["path"] = c["path"]
                                        cookies_to_add.append(entry)
                            elif isinstance(parsed_json, dict):
                                for k, v in parsed_json.items():
                                    cookies_to_add.append({"name": str(k), "value": str(v), "url": prepared_url})
                        except Exception:
                            pass

                    if not cookies_to_add:
                        for part in raw_cookie.split(";"):
                            part = part.strip()
                            if not part:
                                continue
                            if "=" in part:
                                cname, cval = part.split("=", 1)
                                cookies_to_add.append({"name": cname.strip(), "value": cval.strip(), "url": prepared_url})
                            else:
                                cookies_to_add.append({"name": "grafana_session", "value": part.strip(), "url": prepared_url})

                    if cookies_to_add:
                        context.add_cookies(cookies_to_add)
                        bot_log(f"🍪 Injected {len(cookies_to_add)} session cookie(s) into browser context")
                except Exception as cookie_err:
                    bot_log(f"⚠️ Warning adding cookies to context: {cookie_err}")

            page = context.new_page()

            try:
                try:
                    response = page.goto(prepared_url, wait_until="commit", timeout=15000)
                    if response and response.status >= 400:
                        bot_log(f"⚠️ Server returned HTTP status {response.status}")
                    page.wait_for_load_state("domcontentloaded", timeout=10000)
                except Exception as nav_err:
                    err_str = str(nav_err)
                    if "ERR_CONNECTION_REFUSED" in err_str:
                        raise ValueError(f"Connection refused at '{prepared_url}'. Verify the service is running and accessible.")
                    if "Timeout" in err_str:
                        raise TimeoutError(f"Connection timed out (15s) reaching '{prepared_url}'. The VM network/firewall cannot reach this server.")
                    raise RuntimeError(f"Navigation failed: {nav_err}")

                # Wait up to 6 seconds for either login inputs or dashboard elements to show
                try:
                    page.wait_for_selector(
                        "input[type='password'], input[name='user'], input[placeholder*='username' i], input[placeholder*='email' i], button:has-text('Log in'), .react-grid-layout, .dashboard-container, [data-testid='dashboard-content']",
                        timeout=6000
                    )
                except Exception:
                    pass

                # Check if we landed on a login screen
                has_pass_input = page.locator("input[type='password'], input[name='password']").count() > 0
                has_login_btn = page.locator("button:has-text('Log in'), button:has-text('Login'), button[type='submit']").count() > 0
                is_login = "/login" in page.url or (has_pass_input and has_login_btn)

                if is_login:
                    if auth_cookie:
                        bot_log("⚠️ Login screen detected despite session cookie. The session cookie may have expired.")
                    if not user or not pwd:
                        if not auth_cookie and not auth_token:
                            bot_log("⚠️ Grafana login screen detected, but Username / Password / Cookie are not configured in settings!")
                    else:
                        bot_log(f"🔑 Detected login screen. Authenticating as '{user}'...")
                        user_elem = page.locator("input[name='user'], input[id='login-view-username'], input[placeholder*='email' i], input[placeholder*='username' i], input[type='text']").first
                        if user_elem.count() > 0:
                            user_elem.fill(user)
                        time.sleep(0.5)

                        pass_elem = page.locator("input[name='password'], input[id='login-view-password'], input[placeholder*='password' i], input[type='password']").first
                        if pass_elem.count() > 0:
                            pass_elem.fill(pwd)
                        time.sleep(0.5)

                        submit_elem = page.locator("button[type='submit'], button:has-text('Log in'), button:has-text('Login')").first
                        if submit_elem.count() > 0:
                            submit_elem.click()
                            bot_log("⏳ Clicked 'Log in', waiting for session...")

                        try:
                            page.wait_for_load_state("networkidle", timeout=15000)
                        except Exception:
                            pass
                        time.sleep(3)

                        # Skip password change prompt if present
                        try:
                            skip_btn = page.locator("button:has-text('Skip'), a:has-text('Skip')").first
                            if skip_btn.count() > 0:
                                bot_log("⏩ Skipping password change prompt...")
                                skip_btn.click()
                                time.sleep(2)
                        except Exception:
                            pass

                        # Check if still on login page
                        try:
                            if "/login" in page.url:
                                bot_log("⚠️ Still on login screen. Please check if username/password are correct.")
                            elif prepared_url not in page.url:
                                bot_log(f"🌐 Redirecting to target dashboard: {prepared_url}")
                                page.goto(prepared_url, wait_until="domcontentloaded", timeout=30000)
                        except Exception:
                            pass

                bot_log(f"⏳ Waiting for dashboard queries and graphs to finish rendering...")
                try:
                    page.wait_for_load_state("networkidle", timeout=12000)
                except Exception:
                    pass

                # Wait for any "Loading ..." indicator or spinner to disappear
                try:
                    page.wait_for_selector("text=/Loading/i, .panel-loading, .loading-bar", state="hidden", timeout=10000)
                except Exception:
                    pass

                # Wait for actual dashboard panels, panel-106, or content grid to mount
                try:
                    page.wait_for_selector(".react-grid-layout, .panel-content, [data-testid*='panel'], .dashboard-container, .panel-container, [id*='panel'], table", state="visible", timeout=10000)
                except Exception:
                    pass

                time.sleep(self.cfg.PAGE_LOAD_WAIT_SECONDS)

                # Ensure page has settled after any client-side route redirects
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=5000)
                except Exception:
                    pass

                try:
                    page.add_style_tag(content="""
                        .grafana-tooltip, .portal-wrapper { display: none !important; }
                        body { overflow: hidden !important; }
                    """)
                except Exception:
                    pass

                try:
                    if "/d-solo/" in prepared_url or "viewPanel=" in prepared_url:
                        panel = page.locator(".panel-container, .react-grid-item, .panel-content, [data-testid*='panel']").first
                        if panel.count() > 0:
                            panel.screenshot(path=output_path)
                        else:
                            page.screenshot(path=output_path, full_page=False)
                    else:
                        page.screenshot(path=output_path, full_page=False)
                except Exception as shot_err:
                    if "Execution context was destroyed" in str(shot_err) or "navigation" in str(shot_err).lower():
                        bot_log("⚠️ Navigation detected during capture, waiting for page to settle...")
                        time.sleep(3)
                        page.wait_for_load_state("domcontentloaded", timeout=10000)
                        page.screenshot(path=output_path, full_page=False)
                    else:
                        raise shot_err

                file_size = os.path.getsize(output_path)
                bot_log(f"📸 Captured snapshot successfully! ({file_size / 1024:.1f} KB)")
                return output_path

            finally:
                context.close()
                browser.close()


# ==============================================================================
# SLACK 3-STEP S3 UPLOADER
# ==============================================================================
class SlackUploader:
    def __init__(self, token=None, channel_id=None):
        self.token = token or Config.SLACK_BOT_TOKEN
        self.channel_id = channel_id or Config.SLACK_CHANNEL_ID
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def test_auth(self):
        url = "https://slack.com/api/auth.test"
        try:
            resp = requests.post(url, headers=self.headers, timeout=15)
            data = resp.json()
            if data.get("ok"):
                return True, data
            return False, data.get("error", "Unknown auth error")
        except Exception as e:
            return False, str(e)

    def post_text_message(self, message_text):
        url = "https://slack.com/api/chat.postMessage"
        payload = {"channel": self.channel_id, "text": message_text}
        try:
            resp = requests.post(
                url,
                headers={**self.headers, "Content-Type": "application/json; charset=utf-8"},
                data=json.dumps(payload),
                timeout=20
            )
            data = resp.json()
            return data.get("ok", False), data.get("error") if not data.get("ok") else data
        except Exception as e:
            return False, str(e)

    def upload_screenshot(self, image_path, message_text=None):
        if not os.path.exists(image_path):
            return False, f"File not found: {image_path}"

        filename = os.path.basename(image_path)
        file_size = os.path.getsize(image_path)
        comment = message_text or Config.get_formatted_message()

        try:
            resp1 = requests.get(
                "https://slack.com/api/files.getUploadURLExternal",
                headers=self.headers,
                params={"filename": filename, "length": file_size},
                timeout=20
            )
            data1 = resp1.json()
            if not data1.get("ok"):
                return False, f"Slack API Step 1 failed: {data1.get('error')}"

            upload_url = data1["upload_url"]
            file_id = data1["file_id"]

            with open(image_path, "rb") as f:
                file_bytes = f.read()

            resp2 = requests.post(
                upload_url,
                data=file_bytes,
                headers={"Content-Type": "application/octet-stream"},
                timeout=60
            )
            if resp2.status_code not in (200, 201, 204):
                return False, f"Slack S3 upload failed (HTTP {resp2.status_code})"

            resp3 = requests.post(
                "https://slack.com/api/files.completeUploadExternal",
                headers={**self.headers, "Content-Type": "application/json; charset=utf-8"},
                data=json.dumps({
                    "files": [{"id": file_id, "title": f"Grafana Snapshot ({filename})"}],
                    "channel_id": self.channel_id,
                    "initial_comment": comment
                }),
                timeout=30
            )
            data3 = resp3.json()
            if data3.get("ok"):
                return True, data3
            return False, f"Slack Complete failed: {data3.get('error')}"
        except Exception as e:
            return False, str(e)


# ==============================================================================
# LOGS & STATE
# ==============================================================================
LOG_BUFFER = deque(maxlen=150)
BOT_STATE = {
    "is_running": False,
    "is_paused": Config.BOT_PAUSED,
    "last_run_time": None,
    "last_status": "Paused" if Config.BOT_PAUSED else "Idle"
}
_state_lock = threading.Lock()


def bot_log(message):
    timestamp = datetime.now().strftime("%H:%M:%S")
    entry = f"[{timestamp}] {message}"
    print(entry)
    LOG_BUFFER.append(entry)


def execute_cycle():
    with _state_lock:
        if BOT_STATE["is_running"]:
            bot_log("⚠️ Cycle already running, skipping trigger.")
            return
        BOT_STATE["is_running"] = True
        BOT_STATE["last_status"] = "Capturing..."

    image_path = None
    try:
        Config.reload()
        if not Config.GRAFANA_URL:
            bot_log("❌ Grafana Link is empty! Please enter your Grafana URL and click Save.")
            with _state_lock:
                BOT_STATE["last_status"] = "Grafana Link Missing"
            return

        bot_log(f"🚀 Capturing screenshot for: {Config.GRAFANA_URL}")
        capture = GrafanaCapture()
        image_path = capture.capture_screenshot()
        size_kb = os.path.getsize(image_path) / 1024

        if not Config.SLACK_BOT_TOKEN or not Config.SLACK_CHANNEL_ID:
            bot_log(f"⚠️ Screenshot captured successfully ({size_kb:.1f} KB)!")
            bot_log("ℹ️ Slack upload skipped because Slack Bot Token or Channel ID is not configured yet.")
            with _state_lock:
                BOT_STATE["last_run_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                BOT_STATE["last_status"] = f"Captured ({size_kb:.1f} KB) - Slack Pending"
            return

        bot_log(f"📤 Uploading snapshot to Slack channel {Config.SLACK_CHANNEL_ID}...")
        uploader = SlackUploader()
        ok, res = uploader.upload_screenshot(image_path)

        if ok:
            bot_log("✅ Successfully uploaded snapshot to Slack!")
            with _state_lock:
                BOT_STATE["last_run_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                BOT_STATE["last_status"] = "Success"
        else:
            bot_log(f"❌ Slack upload error: {res}")
            with _state_lock:
                BOT_STATE["last_status"] = f"Slack Error: {res}"

    except Exception as e:
        bot_log(f"💥 Capture Error: {e}")
        with _state_lock:
            BOT_STATE["last_status"] = f"Error: {e}"
    finally:
        if image_path and os.path.exists(image_path):
            try:
                os.remove(image_path)
            except Exception:
                pass
        with _state_lock:
            BOT_STATE["is_running"] = False


# ==============================================================================
# STREAMLINED MODERN WEB UI HTML
# ==============================================================================
HTML_TEMPLATE = r'''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Grafana to Slack Bot</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg: #090d16;
      --card-bg: rgba(18, 25, 41, 0.75);
      --border: rgba(255, 255, 255, 0.09);
      --border-focus: #6366f1;
      --primary: #6366f1;
      --primary-hover: #4f46e5;
      --text: #f8fafc;
      --text-muted: #94a3b8;
      --accent-emerald: #10b981;
      --accent-rose: #f43f5e;
      --radius: 14px;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: var(--bg);
      background-image: radial-gradient(at 0% 0%, rgba(99, 102, 241, 0.12) 0px, transparent 50%),
                        radial-gradient(at 100% 100%, rgba(6, 182, 212, 0.08) 0px, transparent 50%);
      background-attachment: fixed;
      color: var(--text);
      font-family: 'Outfit', sans-serif;
      min-height: 100vh;
      display: flex;
      justify-content: center;
      padding: 32px 16px;
    }
    .wrapper { width: 100%; max-width: 760px; }
    
    .header {
      display: flex; justify-content: space-between; align-items: center; margin-bottom: 24px;
    }
    .brand { display: flex; align-items: center; gap: 12px; }
    .brand-icon {
      width: 40px; height: 40px; background: linear-gradient(135deg, #6366f1, #06b6d4);
      border-radius: 10px; display: flex; align-items: center; justify-content: center; font-size: 20px;
    }
    .brand h1 { font-size: 20px; font-weight: 700; }
    .brand p { font-size: 13px; color: var(--text-muted); }

    .status-badge {
      display: inline-flex; align-items: center; gap: 8px; padding: 6px 14px;
      border-radius: 9999px; font-size: 13px; font-weight: 500;
      background: rgba(16, 185, 129, 0.12); border: 1px solid rgba(16, 185, 129, 0.3);
      color: var(--accent-emerald);
    }
    .status-dot {
      width: 8px; height: 8px; border-radius: 50%; background: var(--accent-emerald);
      box-shadow: 0 0 8px var(--accent-emerald);
    }

    .card {
      background: var(--card-bg); border: 1px solid var(--border); border-radius: var(--radius);
      backdrop-filter: blur(16px); padding: 28px; box-shadow: 0 10px 30px rgba(0,0,0,0.3);
      margin-bottom: 24px;
    }

    .form-group { margin-bottom: 18px; }
    label { display: block; font-size: 13px; font-weight: 600; color: var(--text); margin-bottom: 6px; }
    .hint { font-size: 12px; color: var(--text-muted); margin-top: 4px; }
    
    .input {
      width: 100%; background: rgba(9, 13, 22, 0.7); border: 1px solid var(--border);
      border-radius: 8px; color: var(--text); font-family: inherit; font-size: 14px;
      padding: 12px 14px; outline: none; transition: 0.2s ease;
    }
    .input:focus { border-color: var(--border-focus); box-shadow: 0 0 0 3px rgba(99, 102, 241, 0.15); }
    .input-code { font-family: 'JetBrains Mono', monospace; font-size: 13px; }

    .row { display: grid; grid-template-columns: 1fr 140px; gap: 16px; }

    .btn {
      display: inline-flex; align-items: center; justify-content: center; gap: 8px;
      padding: 12px 20px; border-radius: 8px; font-family: inherit; font-size: 14px;
      font-weight: 600; cursor: pointer; transition: 0.2s ease; border: none; outline: none;
    }
    .btn-primary {
      width: 100%; background: linear-gradient(135deg, var(--primary), var(--primary-hover));
      color: #fff; box-shadow: 0 4px 16px rgba(99, 102, 241, 0.35); margin-top: 6px;
    }
    .btn-primary:hover { transform: translateY(-1px); box-shadow: 0 6px 20px rgba(99, 102, 241, 0.5); }
    
    .btn-group { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 10px; margin-top: 14px; }
    .btn-secondary { background: rgba(255, 255, 255, 0.06); border: 1px solid var(--border); color: var(--text); }
    .btn-secondary:hover { background: rgba(255, 255, 255, 0.12); }
    .btn-info { background: rgba(6, 182, 212, 0.15); border: 1px solid rgba(6, 182, 212, 0.3); color: #06b6d4; }
    .btn-info:hover { background: rgba(6, 182, 212, 0.25); }
    .btn-success { background: rgba(16, 185, 129, 0.15); border: 1px solid rgba(16, 185, 129, 0.3); color: var(--accent-emerald); }
    .btn-success:hover { background: rgba(16, 185, 129, 0.25); }

    .terminal-box {
      background: #05070d; border: 1px solid var(--border); border-radius: var(--radius);
      overflow: hidden; font-family: 'JetBrains Mono', monospace; font-size: 12px;
    }
    .terminal-header {
      background: #0c101b; padding: 10px 16px; display: flex; justify-content: space-between;
      color: var(--text-muted); font-size: 11px; border-bottom: 1px solid var(--border);
    }
    .terminal-content { padding: 14px; height: 200px; overflow-y: auto; color: #94a3b8; line-height: 1.6; }
    .terminal-content div { margin-bottom: 3px; }

    .modal-backdrop {
      position: fixed; inset: 0; background: rgba(0, 0, 0, 0.85); backdrop-filter: blur(8px);
      display: none; align-items: center; justify-content: center; z-index: 2000; padding: 20px;
    }
    .modal-box {
      background: #111827; border: 1px solid var(--border); border-radius: var(--radius);
      max-width: 900px; width: 100%; max-height: 90vh; display: flex; flex-direction: column; overflow: hidden;
    }
    .modal-header {
      padding: 14px 20px; display: flex; justify-content: space-between; align-items: center;
      border-bottom: 1px solid var(--border); font-size: 15px; font-weight: 600;
    }
    .modal-body { padding: 20px; overflow-y: auto; text-align: center; }
    .modal-body img { max-width: 100%; border-radius: 8px; border: 1px solid var(--border); }

    .toast {
      position: fixed; bottom: 24px; right: 24px; padding: 12px 18px; border-radius: 8px;
      background: #1e293b; color: #fff; font-size: 13px; display: none; z-index: 3000;
      box-shadow: 0 10px 25px rgba(0,0,0,0.5); border-left: 4px solid var(--primary);
    }
  </style>
</head>
<body>

<div class="wrapper">
  <!-- Header -->
  <div class="header">
    <div class="brand">
      <div class="brand-icon">📈</div>
      <div>
        <h1>Grafana Snapshot Bot</h1>
        <p>Headless Slack Monitoring</p>
      </div>
    </div>
    <div style="display: flex; align-items: center; gap: 10px;">
      <button type="button" class="btn" id="btn-pause-toggle" onclick="togglePause()" style="padding: 6px 14px; font-size: 13px; font-weight: 600; border-radius: 9999px;">
        ⏸ Pause Scheduler
      </button>
      <div class="status-badge">
        <span class="status-dot"></span>
        <span id="status-text">System Idle</span>
      </div>
    </div>
  </div>

  <!-- Settings Card -->
  <div class="card">
    <form id="bot-form" onsubmit="event.preventDefault(); saveAll();">
      <!-- 1. Grafana Link -->
      <div class="form-group">
        <label for="GRAFANA_URL">🌐 Grafana Dashboard Link</label>
        <input type="text" id="GRAFANA_URL" class="input input-code" 
               placeholder="https://cloudwatch.greymatter.greyorange.com/d/xyz/dashboard?kiosk=tv">
        <div class="hint">The direct URL to your Grafana dashboard or panel.</div>
      </div>

      <!-- Grafana Service Account Token (Recommended) -->
      <div class="form-group">
        <label for="GRAFANA_API_TOKEN">🔑 Grafana Service Account Token <span style="color: var(--accent-emerald); font-size: 11px;">(Option 1: API Token)</span></label>
        <input type="password" id="GRAFANA_API_TOKEN" class="input input-code" 
               placeholder="glsa_your_service_account_token_here">
        <div class="hint">Generated in Grafana under <b>Administration → Users and access → Service accounts</b>.</div>
      </div>

      <!-- Grafana Session Cookie (Bypass SSO / Okta / Login Screen) -->
      <div class="form-group">
        <label for="GRAFANA_COOKIE">🍪 Grafana Session Cookie <span style="color: var(--accent-emerald); font-size: 11px;">(Option 2: Recommended for SSO / Google / Okta - Bypasses Login Screen)</span></label>
        <input type="password" id="GRAFANA_COOKIE" class="input input-code" 
               placeholder="grafana_session=abcdef... (or paste full Cookie header / string)">
        <div class="hint">Copy from your logged-in browser (DevTools F12 → Application → Cookies → <code>grafana_session</code>, or Network tab <code>Cookie</code> header).</div>
      </div>

      <!-- Grafana Credentials (Auto-Login Fallback) -->
      <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 18px;">
        <div>
          <label for="GRAFANA_USERNAME">👤 Grafana Username / Email</label>
          <input type="text" id="GRAFANA_USERNAME" class="input" placeholder="admin or email">
          <div class="hint">Optional fallback if token is not used.</div>
        </div>
        <div>
          <label for="GRAFANA_PASSWORD">🔒 Grafana Password</label>
          <input type="password" id="GRAFANA_PASSWORD" class="input" placeholder="••••••••">
          <div class="hint">Optional fallback password.</div>
        </div>
      </div>

      <!-- 2. Slack Auth Token -->
      <div class="form-group">
        <label for="SLACK_BOT_TOKEN">🔑 Slack Bot Auth Token</label>
        <input type="password" id="SLACK_BOT_TOKEN" class="input input-code" 
               placeholder="xoxb-your-slack-bot-token-here">
        <div class="hint">Bot token starting with <code>xoxb-</code> (with <code>files:write</code> scope).</div>
      </div>

      <!-- 3. Slack Channel Link / ID -->
      <div class="form-group">
        <label for="SLACK_CHANNEL_ID">💬 Slack Channel Link or ID</label>
        <input type="text" id="SLACK_CHANNEL_ID" class="input input-code" 
               placeholder="C0123456789 or https://slack.com/archives/C0123456789">
        <div class="hint">Paste either the Channel ID (e.g., C0123456789) or the full channel URL.</div>
      </div>

      <!-- 4. Custom Message & Time Interval -->
      <div class="row">
        <div class="form-group">
          <label for="SLACK_MESSAGE">📝 Custom Message</label>
          <input type="text" id="SLACK_MESSAGE" class="input" 
                 value="📊 *Grafana Snapshot Alert* - {datetime}">
          <div class="hint">Tokens: <code>{datetime}</code>, <code>{date}</code>, <code>{time}</code></div>
        </div>

        <div class="form-group">
          <label for="SCHEDULE_INTERVAL_MINUTES">⏱️ Interval (Mins)</label>
          <input type="number" id="SCHEDULE_INTERVAL_MINUTES" class="input" value="30" min="1">
          <div class="hint">Snapshot frequency.</div>
        </div>
      </div>

      <!-- Save Button -->
      <button type="submit" class="btn btn-primary" id="btn-save">
        💾 Save Settings
      </button>

      <!-- Action Buttons -->
      <div class="btn-group">
        <button type="button" class="btn btn-info" onclick="previewSnapshot()" id="btn-preview">
          📸 Preview Snapshot
        </button>
        <button type="button" class="btn btn-secondary" onclick="testSlack()" id="btn-slack">
          💬 Test Slack
        </button>
        <button type="button" class="btn btn-success" onclick="triggerRun()" id="btn-run">
          ⚡ Run Capture Now
        </button>
      </div>
    </form>
  </div>

  <!-- Real-Time Log Box -->
  <div class="terminal-box">
    <div class="terminal-header">
      <span>📟 Real-Time Activity Log</span>
      <span style="cursor: pointer;" onclick="fetchLogs()">🔄 Refresh</span>
    </div>
    <div class="terminal-content" id="log-output">
      <div>[Ready] Waiting for configuration...</div>
    </div>
  </div>
</div>

<!-- Preview Modal -->
<div class="modal-backdrop" id="preview-modal" onclick="closeModal()">
  <div class="modal-box" onclick="event.stopPropagation()">
    <div class="modal-header">
      <span>📸 Live Headless Capture Preview</span>
      <button type="button" class="btn btn-secondary" style="padding: 4px 10px; font-size: 12px;" onclick="closeModal()">✕ Close</button>
    </div>
    <div class="modal-body" id="modal-body"></div>
  </div>
</div>

<div class="toast" id="toast">Notification</div>

<script>
  function showToast(msg, isError = false) {
    const t = document.getElementById("toast");
    t.innerText = msg;
    t.style.borderLeftColor = isError ? "var(--accent-rose)" : "var(--accent-emerald)";
    t.style.display = "block";
    setTimeout(() => { t.style.display = "none"; }, 4000);
  }

  async function loadData() {
    try {
      const res = await fetch("/api/status");
      const data = await res.json();
      if (data.config) {
        const c = data.config;
        if (c.grafana_url) document.getElementById("GRAFANA_URL").value = c.grafana_url;
        if (c.grafana_token_set) document.getElementById("GRAFANA_API_TOKEN").value = "••••••••••••••••••••••••";
        if (c.grafana_cookie_set) document.getElementById("GRAFANA_COOKIE").value = "••••••••••••••••••••••••";
        if (c.slack_channel_id) document.getElementById("SLACK_CHANNEL_ID").value = c.slack_channel_id;
        if (c.slack_message) document.getElementById("SLACK_MESSAGE").value = c.slack_message;
        if (c.interval) document.getElementById("SCHEDULE_INTERVAL_MINUTES").value = c.interval;
        if (c.slack_token_set) document.getElementById("SLACK_BOT_TOKEN").value = "••••••••••••••••••••••••";
        if (c.grafana_username) document.getElementById("GRAFANA_USERNAME").value = c.grafana_username;
        if (c.grafana_password_set) document.getElementById("GRAFANA_PASSWORD").value = "••••••••••••••••";
      }
      updateStatus(data.bot_state);
    } catch(e) {}
  }

  function updateStatus(state) {
    if (!state) return;
    const text = document.getElementById("status-text");
    const dot = document.querySelector(".status-dot");
    const pauseBtn = document.getElementById("btn-pause-toggle");

    if (state.is_paused) {
      pauseBtn.innerHTML = "▶ Resume Scheduler";
      pauseBtn.style.background = "rgba(16, 185, 129, 0.15)";
      pauseBtn.style.color = "var(--accent-emerald)";
      pauseBtn.style.border = "1px solid rgba(16, 185, 129, 0.4)";
      text.innerText = "Scheduler Paused ⏸️";
      dot.style.background = "#f59e0b";
      dot.style.boxShadow = "0 0 8px #f59e0b";
    } else {
      pauseBtn.innerHTML = "⏸ Pause Scheduler";
      pauseBtn.style.background = "rgba(245, 158, 11, 0.12)";
      pauseBtn.style.color = "#f59e0b";
      pauseBtn.style.border = "1px solid rgba(245, 158, 11, 0.3)";
      if (state.is_running) {
        text.innerText = "Capturing...";
        dot.style.background = "#06b6d4";
        dot.style.boxShadow = "0 0 8px #06b6d4";
      } else {
        text.innerText = state.last_status === "Success" ? `Idle (Last: ${state.last_run_time || 'OK'})` : `Status: ${state.last_status}`;
        dot.style.background = "var(--accent-emerald)";
        dot.style.boxShadow = "0 0 8px var(--accent-emerald)";
      }
    }
  }

  async function togglePause() {
    try {
      const res = await fetch("/api/toggle-pause", { method: "POST" });
      const data = await res.json();
      if (data.success) {
        showToast(data.is_paused ? "⏸️ Automatic captures PAUSED" : "▶️ Automatic captures RESUMED");
        poll();
      }
    } catch(e) {
      showToast("❌ Failed to toggle pause", true);
    }
  }

  async function saveAll() {
    const grafanaUrl = document.getElementById("GRAFANA_URL").value.trim();
    const gToken = document.getElementById("GRAFANA_API_TOKEN").value.trim();
    const gCookie = document.getElementById("GRAFANA_COOKIE").value.trim();
    const gUser = document.getElementById("GRAFANA_USERNAME").value.trim();
    const gPass = document.getElementById("GRAFANA_PASSWORD").value.trim();
    const token = document.getElementById("SLACK_BOT_TOKEN").value.trim();
    const channel = document.getElementById("SLACK_CHANNEL_ID").value.trim();
    const message = document.getElementById("SLACK_MESSAGE").value.trim();
    const interval = document.getElementById("SCHEDULE_INTERVAL_MINUTES").value.trim();

    if (!grafanaUrl) {
      showToast("❌ Please enter your Grafana Link!", true);
      return;
    }

    const payload = {
      GRAFANA_URL: grafanaUrl,
      GRAFANA_USERNAME: gUser,
      SLACK_CHANNEL_ID: channel,
      SLACK_MESSAGE: message,
      SCHEDULE_INTERVAL_MINUTES: interval
    };

    if (gToken && !gToken.startsWith("••••")) {
      payload.GRAFANA_API_TOKEN = gToken;
    } else if (gToken === "") {
      payload.GRAFANA_API_TOKEN = "";
    }

    if (gCookie && !gCookie.startsWith("••••")) {
      payload.GRAFANA_COOKIE = gCookie;
    } else if (gCookie === "") {
      payload.GRAFANA_COOKIE = "";
    }

    if (gPass && !gPass.startsWith("••••")) {
      payload.GRAFANA_PASSWORD = gPass;
    } else if (gPass === "") {
      payload.GRAFANA_PASSWORD = "";
    }

    if (token && !token.startsWith("••••")) {
      payload.SLACK_BOT_TOKEN = token;
    }

    try {
      const res = await fetch("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      const data = await res.json();
      if (data.success) {
        showToast("✅ Settings saved & synced to .env!");
        fetchLogs();
      } else {
        showToast("❌ " + data.error, true);
      }
    } catch(e) {
      showToast("❌ Failed to save settings", true);
    }
  }

  async function previewSnapshot() {
    const url = document.getElementById("GRAFANA_URL").value.trim();
    const gToken = document.getElementById("GRAFANA_API_TOKEN").value.trim();
    const gCookie = document.getElementById("GRAFANA_COOKIE").value.trim();
    const gUser = document.getElementById("GRAFANA_USERNAME").value.trim();
    const gPass = document.getElementById("GRAFANA_PASSWORD").value.trim();

    if (!url) {
      showToast("❌ Please enter a Grafana Link first!", true);
      return;
    }

    const modal = document.getElementById("preview-modal");
    const body = document.getElementById("modal-body");
    body.innerHTML = "<p style='color: #94a3b8; padding: 20px;'>⏳ Launching headless Chromium & rendering charts...</p>";
    modal.style.display = "flex";

    try {
      const res = await fetch("/api/preview-capture", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          url: url,
          token: gToken && !gToken.startsWith("••••") ? gToken : undefined,
          cookie: gCookie && !gCookie.startsWith("••••") ? gCookie : undefined,
          username: gUser,
          password: gPass
        })
      });
      const data = await res.json();
      if (data.success) {
        body.innerHTML = `<img src="${data.image_url}?t=${Date.now()}"><p style="color:var(--accent-emerald); font-size:12px; margin-top:10px;">${data.message}</p>`;
      } else {
        body.innerHTML = `<p style="color: var(--accent-rose); padding: 20px;">❌ Capture Failed: ${data.error}</p>`;
      }
    } catch(e) {
      body.innerHTML = "<p style='color: var(--accent-rose); padding: 20px;'>❌ Network Error during capture.</p>";
    }
    fetchLogs();
  }

  function closeModal() {
    document.getElementById("preview-modal").style.display = "none";
  }

  async function testSlack() {
    const btn = document.getElementById("btn-slack");
    btn.disabled = true;
    btn.innerText = "Connecting...";
    try {
      const res = await fetch("/api/test-slack", { method: "POST" });
      const data = await res.json();
      if (data.success) showToast("✅ " + data.message);
      else showToast("❌ Slack Error: " + data.error, true);
    } catch(e) {
      showToast("❌ Slack connection failed", true);
    } finally {
      btn.disabled = false;
      btn.innerText = "💬 Test Slack";
      fetchLogs();
    }
  }

  async function triggerRun() {
    const btn = document.getElementById("btn-run");
    btn.disabled = true;
    btn.innerText = "Running...";
    try {
      const res = await fetch("/api/trigger", { method: "POST" });
      const data = await res.json();
      if (data.success) showToast("🚀 Capture started! Check log box.");
      else showToast("⚠️ " + data.error, true);
    } catch(e) {
      showToast("❌ Run failed", true);
    } finally {
      setTimeout(() => { btn.disabled = false; btn.innerText = "⚡ Run Capture Now"; }, 3000);
      poll();
    }
  }

  async function fetchLogs() {
    try {
      const res = await fetch("/api/logs");
      const data = await res.json();
      const terminal = document.getElementById("log-output");
      terminal.innerHTML = "";
      (data.logs || []).forEach(l => {
        const div = document.createElement("div");
        div.innerText = l;
        terminal.appendChild(div);
      });
      terminal.scrollTop = terminal.scrollHeight;
    } catch(e) {}
  }

  async function poll() {
    try {
      const res = await fetch("/api/status");
      const data = await res.json();
      updateStatus(data.bot_state);
      fetchLogs();
    } catch(e) {}
  }

  loadData();
  fetchLogs();
  setInterval(poll, 4000);
</script>

</body>
</html>
'''


# ==============================================================================
# FLASK WEB SERVER & APIS
# ==============================================================================
app = Flask(__name__)


@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route("/api/status", methods=["GET"])
def get_status():
    with _state_lock:
        state = dict(BOT_STATE)
    return jsonify({
        "status": "ok",
        "bot_state": state,
        "config": {
            "grafana_url": Config.GRAFANA_URL,
            "grafana_token_set": bool(Config.GRAFANA_API_TOKEN),
            "grafana_cookie_set": bool(Config.GRAFANA_COOKIE),
            "grafana_username": Config.GRAFANA_USERNAME,
            "grafana_password_set": bool(Config.GRAFANA_PASSWORD),
            "slack_channel_id": Config.SLACK_CHANNEL_ID,
            "slack_message": Config.SLACK_MESSAGE,
            "interval": Config.SCHEDULE_INTERVAL_MINUTES,
            "slack_token_set": bool(Config.SLACK_BOT_TOKEN)
        }
    })


@app.route("/api/settings", methods=["POST"])
def update_settings():
    data = request.get_json() or {}
    updates = {}

    if "GRAFANA_URL" in data:
        updates["GRAFANA_URL"] = str(data["GRAFANA_URL"]).strip()

    if "GRAFANA_API_TOKEN" in data:
        val = str(data["GRAFANA_API_TOKEN"]).strip()
        if not val.startswith("••••"):
            updates["GRAFANA_API_TOKEN"] = val

    if "GRAFANA_COOKIE" in data:
        val = str(data["GRAFANA_COOKIE"]).strip()
        if not val.startswith("••••"):
            updates["GRAFANA_COOKIE"] = val

    if "GRAFANA_USERNAME" in data:
        updates["GRAFANA_USERNAME"] = str(data["GRAFANA_USERNAME"]).strip()

    if "GRAFANA_PASSWORD" in data:
        val = str(data["GRAFANA_PASSWORD"]).strip()
        if not val.startswith("••••"):
            updates["GRAFANA_PASSWORD"] = val

    if "SLACK_CHANNEL_ID" in data:
        raw_chan = str(data["SLACK_CHANNEL_ID"]).strip()
        updates["SLACK_CHANNEL_ID"] = Config.parse_channel_id(raw_chan)

    if "SLACK_MESSAGE" in data:
        updates["SLACK_MESSAGE"] = str(data["SLACK_MESSAGE"]).strip()

    if "SCHEDULE_INTERVAL_MINUTES" in data:
        updates["SCHEDULE_INTERVAL_MINUTES"] = str(data["SCHEDULE_INTERVAL_MINUTES"]).strip()

    if "SLACK_BOT_TOKEN" in data:
        val = str(data["SLACK_BOT_TOKEN"]).strip()
        if not val.startswith("••••"):
            updates["SLACK_BOT_TOKEN"] = val

    if updates:
        Config.save_settings(updates)
        bot_log(f"💾 Settings saved to .env (Token: {bool(Config.GRAFANA_API_TOKEN)}, Cookie: {bool(Config.GRAFANA_COOKIE)})")

    return jsonify({"success": True, "message": "Settings saved successfully"})


@app.route("/api/preview-capture", methods=["POST"])
def preview_capture():
    data = request.get_json() or {}
    url = data.get("url") or Config.GRAFANA_URL
    if not url:
        return jsonify({"success": False, "error": "No Grafana URL provided."}), 400

    token = data.get("token")
    if token and token.startswith("••••"):
        token = None

    cookie = data.get("cookie")
    if cookie and cookie.startswith("••••"):
        cookie = None

    username = data.get("username")
    password = data.get("password")
    if password and password.startswith("••••"):
        password = None

    bot_log(f"📸 Live Preview Request for: {url}")
    try:
        capture = GrafanaCapture()
        preview_filename = f"preview_{int(time.time())}.png"
        preview_path = os.path.join(tempfile.gettempdir(), preview_filename)
        capture.capture_screenshot(target_url=url, output_path=preview_path, username=username, password=password, token=token, cookie=cookie)
        size_kb = os.path.getsize(preview_path) / 1024

        return jsonify({
            "success": True,
            "image_url": f"/api/preview-image/{preview_filename}",
            "message": f"Successfully captured snapshot ({size_kb:.1f} KB)"
        })
    except Exception as e:
        bot_log(f"❌ Preview capture failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/preview-image/<filename>", methods=["GET"])
def get_preview_image(filename):
    safe_name = os.path.basename(filename)
    path = os.path.join(tempfile.gettempdir(), safe_name)
    if os.path.exists(path):
        return send_file(path, mimetype="image/png")
    return "Not found", 404


@app.route("/api/toggle-pause", methods=["POST"])
def toggle_pause():
    with _state_lock:
        BOT_STATE["is_paused"] = not BOT_STATE.get("is_paused", False)
        is_paused = BOT_STATE["is_paused"]
        BOT_STATE["last_status"] = "Paused" if is_paused else "Idle"

    Config.save_settings({"BOT_PAUSED": "true" if is_paused else "false"})
    status_str = "PAUSED ⏸️" if is_paused else "RESUMED ▶️"
    bot_log(f"🔄 Automatic scheduler is now {status_str}")
    return jsonify({"success": True, "is_paused": is_paused})


@app.route("/api/trigger", methods=["POST"])
def trigger_cycle():
    if BOT_STATE["is_running"]:
        return jsonify({"success": False, "error": "A cycle is already running."}), 400

    t = threading.Thread(target=execute_cycle, daemon=True)
    t.start()
    return jsonify({"success": True, "message": "Cycle started."})


@app.route("/api/test-slack", methods=["POST"])
def test_slack():
    uploader = SlackUploader()
    ok, details = uploader.test_auth()
    if ok:
        user = details.get("user", "Bot")
        team = details.get("team", "Workspace")
        uploader.post_text_message(
            f"🔔 *Slack Test Ping Successful!*\nConnected as `{user}` on `{team}` at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}."
        )
        return jsonify({
            "success": True,
            "message": f"Connected to '{team}' as '{user}'! Test ping posted to channel."
        })
    return jsonify({"success": False, "error": str(details)}), 400


@app.route("/api/logs", methods=["GET"])
def get_logs():
    return jsonify({"logs": list(LOG_BUFFER)})


def run_web_server():
    bot_log(f"🌐 Web UI Dashboard running at: http://{Config.WEB_HOST}:{Config.WEB_PORT}")
    app.run(host=Config.WEB_HOST, port=Config.WEB_PORT, debug=False, use_reloader=False)


# ==============================================================================
# SCHEDULER & ENTRYPOINT
# ==============================================================================
def scheduler_loop():
    time.sleep(3)
    while True:
        with _state_lock:
            paused = BOT_STATE.get("is_paused", False)

        if not paused:
            try:
                execute_cycle()
            except Exception as e:
                bot_log(f"⚠️ Scheduler error: {e}")

            interval_sec = max(60, Config.SCHEDULE_INTERVAL_MINUTES * 60)
            bot_log(f"⏳ Next scheduled capture in {Config.SCHEDULE_INTERVAL_MINUTES} minute(s)...")

            for _ in range(int(interval_sec / 2)):
                with _state_lock:
                    if BOT_STATE.get("is_paused", False):
                        break
                time.sleep(2)
        else:
            time.sleep(2)


def main():
    parser = argparse.ArgumentParser(description="Headless Grafana to Slack Monitoring Bot")
    parser.add_argument("--once", action="store_true", help="Capture & upload once, then exit")
    args = parser.parse_args()

    Config.reload()

    if args.once:
        execute_cycle()
        return

    bot_log("=" * 65)
    bot_log("🚀 Grafana -> Slack Headless Bot & Operations Center")
    bot_log(f"🌐 Web UI Dashboard: http://{Config.WEB_HOST}:{Config.WEB_PORT}")
    bot_log(f"⏱️  Interval: Every {Config.SCHEDULE_INTERVAL_MINUTES} minute(s)")
    bot_log("=" * 65)

    scheduler_thread = threading.Thread(target=scheduler_loop, daemon=True)
    scheduler_thread.start()

    try:
        run_web_server()
    except KeyboardInterrupt:
        bot_log("\n🛑 Bot stopped.")


if __name__ == "__main__":
    main()
