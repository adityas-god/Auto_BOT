#!/usr/bin/env python3
"""
Grafana to Slack Headless Bot & Operations Center (All-in-One)
- 100% Headless browser capture (Playwright Chromium)
- Modern 3-step Slack S3 Upload API
- Self-contained Flask Web Management UI (Port 5000)
- Multi-dashboard link monitoring synced directly to .env
"""

import os
import sys
import time
import json
import uuid
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

# Ensure .env exists
if not os.path.exists(ENV_PATH):
    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.write("# Headless Bot Configuration\n")

load_dotenv(dotenv_path=ENV_PATH, override=True)


# ==============================================================================
# CONFIGURATION & .ENV MANAGER
# ==============================================================================
class Config:
    GRAFANA_URLS = os.getenv("GRAFANA_URLS", "").strip()
    GRAFANA_URL = os.getenv("GRAFANA_URL", "").strip()
    GRAFANA_USERNAME = os.getenv("GRAFANA_USERNAME", "").strip()
    GRAFANA_PASSWORD = os.getenv("GRAFANA_PASSWORD", "").strip()
    GRAFANA_API_TOKEN = os.getenv("GRAFANA_API_TOKEN", "").strip()
    GRAFANA_COOKIE = os.getenv("GRAFANA_COOKIE", "").strip()

    VIEWPORT_WIDTH = int(os.getenv("VIEWPORT_WIDTH", "1920"))
    VIEWPORT_HEIGHT = int(os.getenv("VIEWPORT_HEIGHT", "1080"))
    PAGE_LOAD_WAIT_SECONDS = int(os.getenv("PAGE_LOAD_WAIT_SECONDS", "8"))
    GRAFANA_THEME = os.getenv("GRAFANA_THEME", "dark").lower().strip()

    SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "").strip()
    SLACK_CHANNEL_ID = os.getenv("SLACK_CHANNEL_ID", "").strip()
    SLACK_THREAD_TS = os.getenv("SLACK_THREAD_TS", "").strip() or None
    SLACK_MESSAGE_TEMPLATE = os.getenv(
        "SLACK_MESSAGE_TEMPLATE",
        "📊 *{title}* - Snapshot at {datetime}\n<{grafana_url}|Open in Grafana>"
    )

    SCHEDULE_INTERVAL_MINUTES = int(os.getenv("SCHEDULE_INTERVAL_MINUTES", "30"))
    TIMEZONE = os.getenv("TIMEZONE", "Asia/Kolkata").strip()
    WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0").strip()
    WEB_PORT = int(os.getenv("WEB_PORT", "5000"))

    @classmethod
    def reload(cls):
        load_dotenv(dotenv_path=ENV_PATH, override=True)
        cls.GRAFANA_URLS = os.getenv("GRAFANA_URLS", "").strip()
        cls.GRAFANA_URL = os.getenv("GRAFANA_URL", "").strip()
        cls.GRAFANA_USERNAME = os.getenv("GRAFANA_USERNAME", "").strip()
        cls.GRAFANA_PASSWORD = os.getenv("GRAFANA_PASSWORD", "").strip()
        cls.GRAFANA_API_TOKEN = os.getenv("GRAFANA_API_TOKEN", "").strip()
        cls.GRAFANA_COOKIE = os.getenv("GRAFANA_COOKIE", "").strip()

        cls.VIEWPORT_WIDTH = int(os.getenv("VIEWPORT_WIDTH", "1920"))
        cls.VIEWPORT_HEIGHT = int(os.getenv("VIEWPORT_HEIGHT", "1080"))
        cls.PAGE_LOAD_WAIT_SECONDS = int(os.getenv("PAGE_LOAD_WAIT_SECONDS", "8"))
        cls.GRAFANA_THEME = os.getenv("GRAFANA_THEME", "dark").lower().strip()

        cls.SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "").strip()
        cls.SLACK_CHANNEL_ID = os.getenv("SLACK_CHANNEL_ID", "").strip()
        cls.SLACK_THREAD_TS = os.getenv("SLACK_THREAD_TS", "").strip() or None
        cls.SLACK_MESSAGE_TEMPLATE = os.getenv(
            "SLACK_MESSAGE_TEMPLATE",
            "📊 *{title}* - Snapshot at {datetime}\n<{grafana_url}|Open in Grafana>"
        )

        cls.SCHEDULE_INTERVAL_MINUTES = int(os.getenv("SCHEDULE_INTERVAL_MINUTES", "30"))
        cls.TIMEZONE = os.getenv("TIMEZONE", "Asia/Kolkata").strip()
        cls.WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0").strip()
        cls.WEB_PORT = int(os.getenv("WEB_PORT", "5000"))

    @classmethod
    def get_links(cls):
        """Returns list of monitored links parsed from GRAFANA_URLS or GRAFANA_URL in .env."""
        cls.reload()
        links = []
        raw_list = []
        if cls.GRAFANA_URLS:
            # Check if JSON format
            if cls.GRAFANA_URLS.startswith("["):
                try:
                    parsed = json.loads(cls.GRAFANA_URLS)
                    if isinstance(parsed, list):
                        return parsed
                except Exception:
                    pass
            # Comma-separated or newline-separated
            raw_list = [u.strip() for u in cls.GRAFANA_URLS.replace("\n", ",").split(",") if u.strip()]
        elif cls.GRAFANA_URL:
            raw_list = [cls.GRAFANA_URL]

        for idx, item in enumerate(raw_list, start=1):
            if "|" in item:
                parts = item.split("|", 1)
                name, url = parts[0].strip(), parts[1].strip()
            else:
                name, url = f"Dashboard {idx}", item
            links.append({
                "id": str(idx),
                "name": name,
                "url": url,
                "enabled": True
            })
        return links

    @classmethod
    def save_links(cls, links_list):
        """Saves links list directly into .env as JSON string under GRAFANA_URLS."""
        # Filter valid links
        clean_links = []
        for l in links_list:
            url = l.get("url", "").strip()
            if url:
                clean_links.append({
                    "id": l.get("id") or str(uuid.uuid4())[:8],
                    "name": l.get("name") or "Dashboard",
                    "url": url,
                    "enabled": l.get("enabled", True)
                })

        json_str = json.dumps(clean_links)
        cls.update_env_key("GRAFANA_URLS", json_str)
        if clean_links:
            cls.update_env_key("GRAFANA_URL", clean_links[0]["url"])
        cls.reload()
        return True

    @classmethod
    def update_env_key(cls, key, value):
        lines = []
        if os.path.exists(ENV_PATH):
            with open(ENV_PATH, "r", encoding="utf-8") as f:
                lines = f.readlines()

        key_found = False
        new_lines = []
        for line in lines:
            if line.strip().startswith(f"{key}=") or line.strip().startswith(f"{key} ="):
                new_lines.append(f"{key}={value}\n")
                key_found = True
            else:
                new_lines.append(line)

        if not key_found:
            new_lines.append(f"{key}={value}\n")

        with open(ENV_PATH, "w", encoding="utf-8") as f:
            f.writelines(new_lines)

    @classmethod
    def save_env_settings(cls, settings_dict):
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
    def get_formatted_message(cls, link_title="", link_url=""):
        try:
            tz = pytz.timezone(cls.TIMEZONE)
            now = datetime.now(tz)
        except Exception:
            now = datetime.now()

        formatted_datetime = now.strftime("%Y-%m-%d %I:%M:%S %p")
        formatted_date = now.strftime("%Y-%m-%d")
        formatted_time = now.strftime("%I:%M:%S %p")

        title = link_title or "Grafana Dashboard"
        url = link_url or cls.GRAFANA_URL or ""

        msg = cls.SLACK_MESSAGE_TEMPLATE
        msg = msg.replace("{title}", title)
        msg = msg.replace("{grafana_url}", url)
        msg = msg.replace("{datetime}", formatted_datetime)
        msg = msg.replace("{date}", formatted_date)
        msg = msg.replace("{time}", formatted_time)
        return msg


# ==============================================================================
# HEADLESS CAPTURE ENGINE (PLAYWRIGHT CHROMIUM)
# ==============================================================================
class GrafanaCapture:
    def __init__(self, config=None):
        self.cfg = config or Config

    def _prepare_url(self, raw_url):
        if not raw_url:
            return ""
        try:
            parsed = urlparse(raw_url)
            query = parse_qs(parsed.query)

            if "/d-solo/" not in parsed.path and "kiosk" not in query:
                query["kiosk"] = ["tv"]

            if "theme" not in query and self.cfg.GRAFANA_THEME:
                query["theme"] = [self.cfg.GRAFANA_THEME]

            new_query = urlencode(query, doseq=True)
            return urlunparse((
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                parsed.params,
                new_query,
                parsed.fragment
            ))
        except Exception:
            return raw_url

    def capture_screenshot(self, target_url=None, output_path=None):
        url_to_capture = target_url or self.cfg.GRAFANA_URL
        if not url_to_capture:
            links = self.cfg.get_links()
            if links:
                url_to_capture = links[0].get("url")

        if not url_to_capture:
            raise ValueError("No Grafana URL provided or configured.")

        if not output_path:
            tmp_dir = tempfile.gettempdir()
            filename = f"grafana_snapshot_{int(time.time() * 1000)}.png"
            output_path = os.path.join(tmp_dir, filename)

        prepared_url = self._prepare_url(url_to_capture)
        bot_log(f"🌐 Navigating headlessly to Grafana: {prepared_url}")

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--hide-scrollbars",
                    "--mute-audio"
                ]
            )

            extra_headers = {}
            if self.cfg.GRAFANA_API_TOKEN:
                extra_headers["Authorization"] = f"Bearer {self.cfg.GRAFANA_API_TOKEN}"

            context = browser.new_context(
                viewport={
                    "width": self.cfg.VIEWPORT_WIDTH,
                    "height": self.cfg.VIEWPORT_HEIGHT
                },
                device_scale_factor=1.0,
                extra_http_headers=extra_headers if extra_headers else None
            )

            if self.cfg.GRAFANA_COOKIE:
                parsed_url = urlparse(prepared_url)
                domain = parsed_url.hostname
                cookie_parts = self.cfg.GRAFANA_COOKIE.split(";")
                cookies_to_add = []
                for part in cookie_parts:
                    if "=" in part:
                        k, v = part.strip().split("=", 1)
                        cookies_to_add.append({
                            "name": k,
                            "value": v,
                            "domain": domain,
                            "path": "/"
                        })
                if cookies_to_add:
                    context.add_cookies(cookies_to_add)

            page = context.new_page()

            try:
                page.goto(prepared_url, wait_until="domcontentloaded", timeout=45000)

                # Check if redirected to login
                current_url = page.url
                if ("/login" in current_url or page.locator("input[name='user']").count() > 0) and self.cfg.GRAFANA_USERNAME:
                    bot_log(f"🔑 Logging into Grafana as user '{self.cfg.GRAFANA_USERNAME}'...")
                    page.fill("input[name='user']", self.cfg.GRAFANA_USERNAME)
                    page.fill("input[name='password']", self.cfg.GRAFANA_PASSWORD)
                    page.click("button[type='submit']")
                    page.wait_for_load_state("networkidle", timeout=30000)

                    if page.locator("text=Skip").count() > 0:
                        page.click("text=Skip")
                        time.sleep(1)

                    if "/d/" in prepared_url or "/d-solo/" in prepared_url:
                        page.goto(prepared_url, wait_until="domcontentloaded", timeout=30000)

                # Rendering buffer
                bot_log(f"⏳ Waiting {self.cfg.PAGE_LOAD_WAIT_SECONDS}s for graphs & queries to render...")
                try:
                    page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass

                time.sleep(self.cfg.PAGE_LOAD_WAIT_SECONDS)

                # Hide unwanted tooltips and scrollbars
                page.add_style_tag(content="""
                    .grafana-tooltip, .portal-wrapper { display: none !important; }
                    body { overflow: hidden !important; }
                """)

                # Capture
                if "/d-solo/" in prepared_url:
                    panel_elem = page.locator(".panel-container, .react-grid-item, .panel-content").first
                    if panel_elem.count() > 0:
                        panel_elem.screenshot(path=output_path)
                    else:
                        page.screenshot(path=output_path, full_page=False)
                else:
                    page.screenshot(path=output_path, full_page=False)

                file_size = os.path.getsize(output_path)
                bot_log(f"📸 Captured snapshot successfully: {output_path} ({file_size / 1024:.1f} KB)")
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

    def post_text_message(self, message_text, thread_ts=None):
        url = "https://slack.com/api/chat.postMessage"
        payload = {
            "channel": self.channel_id,
            "text": message_text,
        }
        if thread_ts or Config.SLACK_THREAD_TS:
            payload["thread_ts"] = thread_ts or Config.SLACK_THREAD_TS

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

    def upload_screenshot(self, image_path, message_text=None, thread_ts=None, link_title=None, link_url=None):
        if not os.path.exists(image_path):
            return False, f"File not found: {image_path}"

        filename = os.path.basename(image_path)
        file_size = os.path.getsize(image_path)
        initial_comment = message_text or Config.get_formatted_message(link_title=link_title, link_url=link_url)
        target_thread = thread_ts or Config.SLACK_THREAD_TS

        # Step 1: Request presigned upload URL
        try:
            resp1 = requests.get(
                "https://slack.com/api/files.getUploadURLExternal",
                headers=self.headers,
                params={"filename": filename, "length": file_size},
                timeout=20
            )
            data1 = resp1.json()
            if not data1.get("ok"):
                return False, f"Step 1 failed: {data1.get('error')}"

            upload_url = data1["upload_url"]
            file_id = data1["file_id"]

            # Step 2: Binary S3 upload
            with open(image_path, "rb") as f:
                file_bytes = f.read()

            resp2 = requests.post(
                upload_url,
                data=file_bytes,
                headers={"Content-Type": "application/octet-stream"},
                timeout=60
            )
            if resp2.status_code not in (200, 201, 204):
                return False, f"Step 2 failed with status {resp2.status_code}"

            # Step 3: Complete upload
            title_text = f"Grafana - {link_title}" if link_title else f"Grafana ({filename})"
            step3_payload = {
                "files": [{"id": file_id, "title": title_text}],
                "channel_id": self.channel_id,
                "initial_comment": initial_comment
            }
            if target_thread:
                step3_payload["thread_ts"] = target_thread

            resp3 = requests.post(
                "https://slack.com/api/files.completeUploadExternal",
                headers={**self.headers, "Content-Type": "application/json; charset=utf-8"},
                data=json.dumps(step3_payload),
                timeout=30
            )
            data3 = resp3.json()
            if data3.get("ok"):
                return True, data3
            return False, f"Step 3 failed: {data3.get('error')}"
        except Exception as e:
            return False, str(e)


# ==============================================================================
# LOGS & SYSTEM STATE
# ==============================================================================
LOG_BUFFER = deque(maxlen=150)
BOT_STATE = {
    "is_running_cycle": False,
    "last_run_time": None,
    "last_run_status": "Idle",
    "last_run_details": []
}
_state_lock = threading.Lock()


def bot_log(message):
    timestamp = datetime.now().strftime("%H:%M:%S")
    entry = f"[{timestamp}] {message}"
    print(entry)
    LOG_BUFFER.append(entry)


def execute_cycle_task():
    with _state_lock:
        if BOT_STATE["is_running_cycle"]:
            bot_log("⚠️ Cycle already running, skipping trigger.")
            return
        BOT_STATE["is_running_cycle"] = True
        BOT_STATE["last_run_status"] = "Capturing..."

    try:
        Config.reload()
        bot_log("🚀 Starting snapshot cycle for all active Grafana links...")
        links = Config.get_links()
        active_links = [l for l in links if l.get("enabled", True) and l.get("url", "").strip()]

        if not active_links:
            bot_log("⚠️ No active Grafana links configured to monitor.")
            with _state_lock:
                BOT_STATE["last_run_status"] = "No Links Configured"
            return

        if not Config.SLACK_BOT_TOKEN or not Config.SLACK_CHANNEL_ID:
            bot_log("❌ Slack credentials missing in .env.")
            with _state_lock:
                BOT_STATE["last_run_status"] = "Slack Config Missing"
            return

        capture_engine = GrafanaCapture()
        uploader = SlackUploader()
        results = []

        for idx, link in enumerate(active_links, start=1):
            name = link.get("name") or f"Dashboard {idx}"
            url = link.get("url")
            bot_log(f"📸 [{idx}/{len(active_links)}] Snapping '{name}'...")

            image_path = None
            try:
                image_path = capture_engine.capture_screenshot(target_url=url)
                bot_log(f"   Posting '{name}' to Slack...")
                ok, res = uploader.upload_screenshot(image_path, link_title=name, link_url=url)
                if ok:
                    bot_log(f"   ✅ '{name}' posted successfully!")
                    results.append({"name": name, "success": True})
                else:
                    bot_log(f"   ❌ Slack error for '{name}': {res}")
                    results.append({"name": name, "success": False, "error": str(res)})
            except Exception as ex:
                bot_log(f"   ❌ Capture error for '{name}': {ex}")
                results.append({"name": name, "success": False, "error": str(ex)})
            finally:
                if image_path and os.path.exists(image_path):
                    try:
                        os.remove(image_path)
                    except Exception:
                        pass

        success_count = sum(1 for r in results if r["success"])
        bot_log(f"🏁 Completed cycle: {success_count}/{len(active_links)} snapshots sent.")

        with _state_lock:
            BOT_STATE["last_run_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            BOT_STATE["last_run_status"] = "Success" if success_count == len(active_links) else "Partial/Failed"
            BOT_STATE["last_run_details"] = results

    except Exception as e:
        bot_log(f"💥 Error in cycle: {e}")
        with _state_lock:
            BOT_STATE["last_run_status"] = f"Error: {e}"
    finally:
        with _state_lock:
            BOT_STATE["is_running_cycle"] = False


# ==============================================================================
# EMBEDDED WEB UI HTML TEMPLATE
# ==============================================================================
HTML_TEMPLATE = r'''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Grafana Headless Bot - Cloud Operations Center</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg-base: #0a0d14;
      --bg-surface: #111726;
      --bg-card: rgba(22, 30, 49, 0.7);
      --border-card: rgba(255, 255, 255, 0.08);
      --border-focus: #6366f1;
      --primary: #6366f1;
      --primary-hover: #4f46e5;
      --primary-glow: rgba(99, 102, 241, 0.35);
      --accent-cyan: #06b6d4;
      --accent-emerald: #10b981;
      --accent-amber: #f59e0b;
      --accent-rose: #f43f5e;
      --text-main: #f8fafc;
      --text-muted: #94a3b8;
      --text-dim: #64748b;
      --radius-sm: 8px;
      --radius-md: 14px;
      --radius-lg: 20px;
      --transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background-color: var(--bg-base);
      background-image: 
        radial-gradient(at 0% 0%, rgba(99, 102, 241, 0.12) 0px, transparent 50%),
        radial-gradient(at 100% 100%, rgba(6, 182, 212, 0.08) 0px, transparent 50%);
      background-attachment: fixed;
      color: var(--text-main);
      font-family: 'Outfit', sans-serif;
      min-height: 100vh;
    }
    .container { max-width: 1280px; margin: 0 auto; padding: 24px 20px 48px; width: 100%; }
    header {
      display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 16px;
      padding: 16px 24px; background: var(--bg-card); border: 1px solid var(--border-card);
      border-radius: var(--radius-lg); backdrop-filter: blur(16px); box-shadow: 0 10px 30px rgba(0,0,0,0.3);
      margin-bottom: 24px;
    }
    .brand { display: flex; align-items: center; gap: 14px; }
    .brand-icon {
      width: 44px; height: 44px; background: linear-gradient(135deg, #6366f1, #06b6d4);
      border-radius: var(--radius-sm); display: flex; align-items: center; justify-content: center;
      font-size: 22px; box-shadow: 0 4px 16px var(--primary-glow);
    }
    .brand-title h1 { font-size: 20px; font-weight: 700; letter-spacing: -0.02em; }
    .brand-title p { font-size: 13px; color: var(--text-muted); }
    .header-actions { display: flex; align-items: center; gap: 12px; }
    .status-badge {
      display: inline-flex; align-items: center; gap: 8px; padding: 6px 14px; border-radius: 9999px;
      font-size: 13px; font-weight: 500; background: rgba(16, 185, 129, 0.12);
      border: 1px solid rgba(16, 185, 129, 0.3); color: var(--accent-emerald);
    }
    .status-dot {
      width: 8px; height: 8px; border-radius: 50%; background: var(--accent-emerald);
      box-shadow: 0 0 10px var(--accent-emerald); animation: pulse 2s infinite;
    }
    @keyframes pulse { 0%, 100% { opacity: 1; transform: scale(1); } 50% { opacity: 0.4; transform: scale(0.85); } }
    .btn {
      display: inline-flex; align-items: center; justify-content: center; gap: 8px;
      padding: 10px 18px; border-radius: var(--radius-sm); font-family: inherit; font-size: 14px;
      font-weight: 600; cursor: pointer; transition: var(--transition); border: none; outline: none;
    }
    .btn-primary { background: linear-gradient(135deg, var(--primary), var(--primary-hover)); color: #fff; box-shadow: 0 4px 16px var(--primary-glow); }
    .btn-primary:hover { transform: translateY(-1px); box-shadow: 0 6px 20px rgba(99, 102, 241, 0.5); }
    .btn-secondary { background: rgba(255, 255, 255, 0.05); border: 1px solid var(--border-card); color: var(--text-main); }
    .btn-secondary:hover { background: rgba(255, 255, 255, 0.1); border-color: rgba(255, 255, 255, 0.15); }
    .btn-danger { background: rgba(244, 63, 94, 0.12); border: 1px solid rgba(244, 63, 94, 0.3); color: var(--accent-rose); }
    .btn-danger:hover { background: rgba(244, 63, 94, 0.2); }
    .btn-sm { padding: 6px 12px; font-size: 12px; border-radius: 6px; }
    .dashboard-grid { display: grid; grid-template-columns: 1.4fr 1fr; gap: 24px; }
    @media (max-width: 992px) { .dashboard-grid { grid-template-columns: 1fr; } }
    .card {
      background: var(--bg-card); border: 1px solid var(--border-card); border-radius: var(--radius-lg);
      backdrop-filter: blur(16px); padding: 24px; box-shadow: 0 10px 30px rgba(0,0,0,0.25); margin-bottom: 24px;
    }
    .card-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; padding-bottom: 12px; border-bottom: 1px solid var(--border-card); }
    .card-title { font-size: 17px; font-weight: 600; display: flex; align-items: center; gap: 10px; }
    .link-list { display: flex; flex-direction: column; gap: 14px; }
    .link-item {
      background: rgba(17, 23, 38, 0.8); border: 1px solid var(--border-card); border-radius: var(--radius-md);
      padding: 16px; display: flex; flex-direction: column; gap: 10px; transition: var(--transition);
    }
    .link-item:hover { border-color: rgba(99, 102, 241, 0.4); box-shadow: 0 4px 16px rgba(0,0,0,0.2); }
    .link-item-top { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
    .link-name-input {
      font-weight: 600; font-size: 15px; background: transparent; border: none; color: var(--text-main);
      border-bottom: 1px dashed rgba(255, 255, 255, 0.2); padding: 2px 4px; flex: 1; outline: none;
    }
    .link-name-input:focus { border-bottom: 1px solid var(--primary); }
    .link-url-input {
      width: 100%; background: rgba(10, 13, 20, 0.6); border: 1px solid var(--border-card);
      border-radius: var(--radius-sm); color: var(--text-muted); font-family: 'JetBrains Mono', monospace;
      font-size: 12px; padding: 10px 12px; outline: none; transition: var(--transition);
    }
    .link-url-input:focus { border-color: var(--border-focus); color: var(--text-main); }
    .link-actions { display: flex; justify-content: flex-end; gap: 8px; }
    .form-group { margin-bottom: 16px; }
    .form-row { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
    label { display: block; font-size: 13px; font-weight: 500; color: var(--text-muted); margin-bottom: 6px; }
    .form-input {
      width: 100%; background: rgba(10, 13, 20, 0.6); border: 1px solid var(--border-card);
      border-radius: var(--radius-sm); color: var(--text-main); font-family: inherit; font-size: 14px;
      padding: 10px 14px; outline: none; transition: var(--transition);
    }
    .form-input:focus { border-color: var(--border-focus); box-shadow: 0 0 0 3px rgba(99, 102, 241, 0.15); }
    .input-hint { font-size: 11px; color: var(--text-dim); margin-top: 4px; }
    .terminal-window {
      background: #06080e; border: 1px solid rgba(255, 255, 255, 0.08); border-radius: var(--radius-md);
      overflow: hidden; font-family: 'JetBrains Mono', monospace; font-size: 12px; margin-top: 14px;
    }
    .terminal-bar {
      background: #0d121d; padding: 8px 14px; display: flex; justify-content: space-between;
      align-items: center; border-bottom: 1px solid rgba(255, 255, 255, 0.05); color: var(--text-dim); font-size: 11px;
    }
    .terminal-dots { display: flex; gap: 6px; }
    .terminal-dot { width: 10px; height: 10px; border-radius: 50%; }
    .terminal-content { padding: 14px; height: 220px; overflow-y: auto; color: #94a3b8; line-height: 1.6; }
    .terminal-content div { margin-bottom: 4px; }
    .modal-backdrop {
      position: fixed; inset: 0; background: rgba(0, 0, 0, 0.8); backdrop-filter: blur(8px);
      display: none; align-items: center; justify-content: center; z-index: 1000; padding: 20px;
    }
    .modal-box {
      background: var(--bg-surface); border: 1px solid var(--border-card); border-radius: var(--radius-lg);
      max-width: 900px; width: 100%; max-height: 90vh; display: flex; flex-direction: column;
      overflow: hidden; box-shadow: 0 20px 50px rgba(0,0,0,0.6);
    }
    .modal-header { padding: 16px 20px; display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid var(--border-card); }
    .modal-body { padding: 20px; overflow-y: auto; text-align: center; }
    .modal-body img { max-width: 100%; border-radius: var(--radius-sm); border: 1px solid var(--border-card); }
    .toast {
      position: fixed; bottom: 24px; right: 24px; padding: 14px 20px; border-radius: var(--radius-sm);
      background: #1e293b; color: #fff; font-size: 14px; box-shadow: 0 10px 25px rgba(0,0,0,0.5);
      border-left: 4px solid var(--primary); display: none; z-index: 2000;
    }
  </style>
</head>
<body>
<div class="container">
  <header>
    <div class="brand">
      <div class="brand-icon">📈</div>
      <div class="brand-title">
        <h1>Grafana Headless Bot</h1>
        <p>Operations & Snapshot Control Hub</p>
      </div>
    </div>
    <div class="header-actions">
      <div class="status-badge" id="system-status">
        <span class="status-dot"></span>
        <span id="status-text">System Idle</span>
      </div>
      <button class="btn btn-secondary" onclick="testSlackConnection()" id="btn-slack-test">💬 Test Slack</button>
      <button class="btn btn-primary" onclick="triggerFullCycle()" id="btn-run-cycle">⚡ Run Capture Now</button>
    </div>
  </header>

  <div class="dashboard-grid">
    <div>
      <div class="card">
        <div class="card-header">
          <div class="card-title"><span>🔗</span> Monitored Grafana Links</div>
          <button class="btn btn-secondary btn-sm" onclick="addNewLink()">+ Add Link</button>
        </div>
        <p style="font-size: 13px; color: var(--text-muted); margin-bottom: 16px;">
          Add Grafana dashboards or panel links. All enabled URLs are snapped & posted to Slack automatically.
        </p>
        <div class="link-list" id="links-container"></div>
        <div style="margin-top: 20px; display: flex; justify-content: flex-end;">
          <button class="btn btn-primary" onclick="saveLinks()">💾 Save All Links</button>
        </div>
      </div>

      <div class="card">
        <div class="card-header">
          <div class="card-title"><span>📟</span> Real-Time Activity Log</div>
          <button class="btn btn-secondary btn-sm" onclick="fetchLogs()">🔄 Refresh</button>
        </div>
        <div class="terminal-window">
          <div class="terminal-bar">
            <div class="terminal-dots">
              <span class="terminal-dot" style="background:#f43f5e"></span>
              <span class="terminal-dot" style="background:#f59e0b"></span>
              <span class="terminal-dot" style="background:#10b981"></span>
            </div>
            <span>bot-runner.log</span>
          </div>
          <div class="terminal-content" id="log-output"></div>
        </div>
      </div>
    </div>

    <div>
      <div class="card">
        <div class="card-header">
          <div class="card-title"><span>⚙️</span> Settings & Credentials (.env)</div>
        </div>
        <form id="settings-form" onsubmit="event.preventDefault(); saveSettings();">
          <div class="form-group">
            <label for="SLACK_BOT_TOKEN">Slack Bot Token (xoxb-...)</label>
            <input type="password" id="SLACK_BOT_TOKEN" class="form-input" placeholder="xoxb-your-token-here">
            <div class="input-hint">Requires files:write, chat:write scopes</div>
          </div>
          <div class="form-row">
            <div class="form-group">
              <label for="SLACK_CHANNEL_ID">Slack Channel ID</label>
              <input type="text" id="SLACK_CHANNEL_ID" class="form-input" placeholder="C0123456789">
            </div>
            <div class="form-group">
              <label for="SCHEDULE_INTERVAL_MINUTES">Interval (Minutes)</label>
              <input type="number" id="SCHEDULE_INTERVAL_MINUTES" class="form-input" value="30" min="1">
            </div>
          </div>
          <div class="form-group">
            <label for="SLACK_MESSAGE_TEMPLATE">Slack Message Template</label>
            <input type="text" id="SLACK_MESSAGE_TEMPLATE" class="form-input" placeholder="📊 *{title}* - Snapshot at {datetime}">
            <div class="input-hint">Tokens: {title}, {grafana_url}, {datetime}, {date}, {time}</div>
          </div>
          <div class="form-row">
            <div class="form-group">
              <label for="TIMEZONE">Timezone</label>
              <input type="text" id="TIMEZONE" class="form-input" value="Asia/Kolkata">
            </div>
            <div class="form-group">
              <label for="WEB_PORT">Web UI Port</label>
              <input type="number" id="WEB_PORT" class="form-input" value="5000">
            </div>
          </div>
          <hr style="border: 0; border-top: 1px solid var(--border-card); margin: 20px 0;">
          <div class="card-title" style="margin-bottom: 16px; font-size: 15px;">
            <span>🛡️</span> Grafana Auth & Rendering
          </div>
          <div class="form-row">
            <div class="form-group">
              <label for="GRAFANA_USERNAME">Grafana Username</label>
              <input type="text" id="GRAFANA_USERNAME" class="form-input" placeholder="admin">
            </div>
            <div class="form-group">
              <label for="GRAFANA_PASSWORD">Grafana Password</label>
              <input type="password" id="GRAFANA_PASSWORD" class="form-input" placeholder="••••••••">
            </div>
          </div>
          <div class="form-group">
            <label for="GRAFANA_API_TOKEN">Alternative: Service Account / Token</label>
            <input type="password" id="GRAFANA_API_TOKEN" class="form-input" placeholder="glsa_...">
          </div>
          <div class="form-row">
            <div class="form-group">
              <label for="VIEWPORT_WIDTH">Viewport Width (px)</label>
              <input type="number" id="VIEWPORT_WIDTH" class="form-input" value="1920">
            </div>
            <div class="form-group">
              <label for="VIEWPORT_HEIGHT">Viewport Height (px)</label>
              <input type="number" id="VIEWPORT_HEIGHT" class="form-input" value="1080">
            </div>
          </div>
          <div class="form-row">
            <div class="form-group">
              <label for="PAGE_LOAD_WAIT_SECONDS">Render Wait Buffer (s)</label>
              <input type="number" id="PAGE_LOAD_WAIT_SECONDS" class="form-input" value="8" min="2">
            </div>
            <div class="form-group">
              <label for="GRAFANA_THEME">Theme</label>
              <select id="GRAFANA_THEME" class="form-input">
                <option value="dark">Dark</option>
                <option value="light">Light</option>
              </select>
            </div>
          </div>
          <div style="margin-top: 24px;">
            <button type="submit" class="btn btn-primary" style="width: 100%;">💾 Save Settings & Update .env</button>
          </div>
        </form>
      </div>
    </div>
  </div>
</div>

<div class="modal-backdrop" id="preview-modal" onclick="closeModal(event)">
  <div class="modal-box" onclick="event.stopPropagation()">
    <div class="modal-header">
      <h3 id="modal-title" style="font-size: 16px;">📸 Snapshot Preview</h3>
      <button class="btn btn-secondary btn-sm" onclick="closeModal()">✕ Close</button>
    </div>
    <div class="modal-body" id="modal-body"></div>
  </div>
</div>
<div class="toast" id="toast">Notification</div>

<script>
  let links = [];

  function showToast(msg, isError = false) {
    const toast = document.getElementById("toast");
    toast.innerText = msg;
    toast.style.borderLeftColor = isError ? "var(--accent-rose)" : "var(--accent-emerald)";
    toast.style.display = "block";
    setTimeout(() => { toast.style.display = "none"; }, 4000);
  }

  async function loadLinks() {
    try {
      const res = await fetch("/api/links");
      const data = await res.json();
      links = data.links || [];
      if (links.length === 0) links = [{ id: "1", name: "Main Dashboard", url: "", enabled: true }];
      renderLinks();
    } catch (e) { showToast("Error loading links", true); }
  }

  function renderLinks() {
    const container = document.getElementById("links-container");
    container.innerHTML = "";
    links.forEach((link, idx) => {
      const div = document.createElement("div");
      div.className = "link-item";
      div.innerHTML = `
        <div class="link-item-top">
          <input type="text" class="link-name-input" value="${escapeHtml(link.name || 'Dashboard ' + (idx + 1))}" 
                 onchange="links[${idx}].name = this.value">
          <div style="display: flex; align-items: center; gap: 8px;">
            <label style="margin:0; font-size:12px; cursor:pointer;">
              <input type="checkbox" ${link.enabled !== false ? 'checked' : ''} 
                     onchange="links[${idx}].enabled = this.checked"> Active
            </label>
            <button class="btn btn-danger btn-sm" onclick="deleteLink(${idx})">🗑️</button>
          </div>
        </div>
        <input type="url" class="link-url-input" value="${escapeHtml(link.url || '')}" 
               placeholder="https://grafana.example.com/d/xyz?kiosk=tv"
               onchange="links[${idx}].url = this.value">
        <div class="link-actions">
          <button class="btn btn-secondary btn-sm" onclick="testCaptureSingle(${idx})">👁️ Preview Capture</button>
        </div>
      `;
      container.appendChild(div);
    });
  }

  function addNewLink() {
    links.push({ id: Math.random().toString(36).substring(2, 9), name: `Dashboard ${links.length + 1}`, url: "", enabled: true });
    renderLinks();
  }

  function deleteLink(idx) {
    if (links.length <= 1) { links[0].url = ""; links[0].name = "Dashboard 1"; renderLinks(); return; }
    links.splice(idx, 1);
    renderLinks();
  }

  async function saveLinks() {
    const items = document.querySelectorAll(".link-item");
    const collected = [];
    items.forEach((item, idx) => {
      const name = (item.querySelector(".link-name-input").value || "").trim() || `Dashboard ${idx + 1}`;
      const url = (item.querySelector(".link-url-input").value || "").trim();
      const enabled = item.querySelector("input[type='checkbox']").checked;
      collected.push({
        id: (links[idx] && links[idx].id) || String(idx + 1),
        name: name,
        url: url,
        enabled: enabled
      });
    });

    const hasValid = collected.some(l => l.url.length > 0 && l.enabled);
    if (!hasValid) {
      showToast("❌ Please type a valid Grafana URL in the box before saving!", true);
      return;
    }

    links = collected;
    try {
      const res = await fetch("/api/links", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ links: collected })
      });
      const data = await res.json();
      if (data.success) {
        showToast("✅ Links saved & synced to .env!");
        fetchLogs();
      } else {
        showToast("❌ " + (data.error || "Failed"), true);
      }
    } catch (e) {
      showToast("❌ Error saving links", true);
    }
  }

  async function loadSettings() {
    try {
      const res = await fetch("/api/status");
      const data = await res.json();
      if (data.config) {
        const c = data.config;
        if (c.slack_channel_id) document.getElementById("SLACK_CHANNEL_ID").value = c.slack_channel_id;
        if (c.schedule_interval_minutes) document.getElementById("SCHEDULE_INTERVAL_MINUTES").value = c.schedule_interval_minutes;
        if (c.timezone) document.getElementById("TIMEZONE").value = c.timezone;
        if (c.web_port) document.getElementById("WEB_PORT").value = c.web_port;
        if (c.viewport_width) document.getElementById("VIEWPORT_WIDTH").value = c.viewport_width;
        if (c.viewport_height) document.getElementById("VIEWPORT_HEIGHT").value = c.viewport_height;
        if (c.page_load_wait_seconds) document.getElementById("PAGE_LOAD_WAIT_SECONDS").value = c.page_load_wait_seconds;
        if (c.theme) document.getElementById("GRAFANA_THEME").value = c.theme;
        if (c.slack_message_template) document.getElementById("SLACK_MESSAGE_TEMPLATE").value = c.slack_message_template;
        if (c.slack_bot_token_set) document.getElementById("SLACK_BOT_TOKEN").value = "••••••••••••••••";
        if (c.grafana_username_set) document.getElementById("GRAFANA_USERNAME").value = "admin";
      }
      updateStatusDisplay(data.bot_state);
    } catch (e) {}
  }

  function updateStatusDisplay(botState) {
    const dot = document.querySelector(".status-dot");
    const text = document.getElementById("status-text");
    if (!botState) return;
    if (botState.is_running_cycle) {
      text.innerText = "Capturing & Posting...";
      dot.style.background = "var(--accent-cyan)";
      dot.style.boxShadow = "0 0 10px var(--accent-cyan)";
    } else {
      text.innerText = botState.last_run_status === "Success" ? `Idle (Last: ${botState.last_run_time || 'Done'})` : `Status: ${botState.last_run_status || 'Idle'}`;
      dot.style.background = "var(--accent-emerald)";
      dot.style.boxShadow = "0 0 10px var(--accent-emerald)";
    }
  }

  async function saveSettings() {
    const keys = ["SLACK_BOT_TOKEN", "SLACK_CHANNEL_ID", "SCHEDULE_INTERVAL_MINUTES", "SLACK_MESSAGE_TEMPLATE", "TIMEZONE", "WEB_PORT", "GRAFANA_USERNAME", "GRAFANA_PASSWORD", "GRAFANA_API_TOKEN", "VIEWPORT_WIDTH", "VIEWPORT_HEIGHT", "PAGE_LOAD_WAIT_SECONDS", "GRAFANA_THEME"];
    const payload = {};
    keys.forEach(k => {
      const el = document.getElementById(k);
      if (el && el.value !== "") payload[k] = el.value;
    });

    try {
      const res = await fetch("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      const data = await res.json();
      if (data.success) { showToast("✅ Settings updated in .env!"); fetchLogs(); }
    } catch (e) { showToast("❌ Failed to save settings", true); }
  }

  async function triggerFullCycle() {
    const btn = document.getElementById("btn-run-cycle");
    btn.disabled = true;
    btn.innerText = "⏳ Running...";
    try {
      const res = await fetch("/api/trigger", { method: "POST" });
      const data = await res.json();
      if (data.success) showToast("🚀 Cycle initiated for all links!");
    } catch (e) { showToast("❌ Trigger failed", true); }
    finally {
      setTimeout(() => { btn.disabled = false; btn.innerText = "⚡ Run Capture Now"; }, 3000);
      pollStatus();
    }
  }

  async function testSlackConnection() {
    const btn = document.getElementById("btn-slack-test");
    btn.disabled = true;
    btn.innerText = "Testing...";
    try {
      const res = await fetch("/api/test-slack", { method: "POST" });
      const data = await res.json();
      if (data.success) showToast(data.message);
      else showToast("❌ " + data.error, true);
    } catch (e) { showToast("❌ Slack test error", true); }
    finally {
      btn.disabled = false;
      btn.innerText = "💬 Test Slack";
      fetchLogs();
    }
  }

  async function testCaptureSingle(idx) {
    const link = links[idx];
    if (!link || !link.url) { showToast("❌ Enter URL first!", true); return; }
    const modal = document.getElementById("preview-modal");
    const modalBody = document.getElementById("modal-body");
    const modalTitle = document.getElementById("modal-title");
    modalTitle.innerText = `📸 Capturing: ${link.name || 'Dashboard'}`;
    modalBody.innerHTML = `<p style="color: var(--text-muted);">⏳ Launching headless browser & rendering ${escapeHtml(link.url)}...</p>`;
    modal.style.display = "flex";

    try {
      const res = await fetch("/api/test-capture", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: link.url })
      });
      const data = await res.json();
      if (data.success) {
        modalBody.innerHTML = `<img src="${data.preview_url}?t=${Date.now()}"><p style="font-size:12px; color:var(--accent-emerald); margin-top:10px;">${data.message}</p>`;
      } else {
        modalBody.innerHTML = `<p style="color: var(--accent-rose);">❌ ${escapeHtml(data.error)}</p>`;
      }
    } catch (e) { modalBody.innerHTML = `<p style="color: var(--accent-rose);">❌ Capture error.</p>`; }
    fetchLogs();
  }

  function closeModal() { document.getElementById("preview-modal").style.display = "none"; }

  async function fetchLogs() {
    try {
      const res = await fetch("/api/logs");
      const data = await res.json();
      const terminal = document.getElementById("log-output");
      terminal.innerHTML = "";
      (data.logs || []).forEach(line => {
        const d = document.createElement("div");
        d.innerText = line;
        terminal.appendChild(d);
      });
      terminal.scrollTop = terminal.scrollHeight;
    } catch (e) {}
  }

  async function pollStatus() {
    try {
      const res = await fetch("/api/status");
      const data = await res.json();
      updateStatusDisplay(data.bot_state);
      fetchLogs();
    } catch (e) {}
  }

  function escapeHtml(str) {
    if (!str) return '';
    return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  loadLinks();
  loadSettings();
  fetchLogs();
  setInterval(pollStatus, 4000);
</script>
</body>
</html>
'''


# ==============================================================================
# FLASK WEB APPLICATION & APIS
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
            "schedule_interval_minutes": Config.SCHEDULE_INTERVAL_MINUTES,
            "timezone": Config.TIMEZONE,
            "slack_channel_id": Config.SLACK_CHANNEL_ID,
            "slack_bot_token_set": bool(Config.SLACK_BOT_TOKEN),
            "grafana_username_set": bool(Config.GRAFANA_USERNAME),
            "viewport_width": Config.VIEWPORT_WIDTH,
            "viewport_height": Config.VIEWPORT_HEIGHT,
            "page_load_wait_seconds": Config.PAGE_LOAD_WAIT_SECONDS,
            "theme": Config.GRAFANA_THEME,
            "web_port": Config.WEB_PORT,
            "slack_message_template": Config.SLACK_MESSAGE_TEMPLATE
        }
    })


@app.route("/api/links", methods=["GET", "POST"])
def manage_links():
    if request.method == "GET":
        return jsonify({"links": Config.get_links()})

    data = request.get_json() or {}
    new_links = data.get("links", [])
    if Config.save_links(new_links):
        bot_log(f"💾 Updated monitored links list ({len(new_links)} link(s) saved directly in .env).")
        return jsonify({"success": True, "links": Config.get_links()})
    return jsonify({"success": False, "error": "Failed to save links"}), 500


@app.route("/api/settings", methods=["POST"])
def update_settings():
    data = request.get_json() or {}
    allowed_keys = [
        "GRAFANA_USERNAME", "GRAFANA_PASSWORD", "GRAFANA_API_TOKEN", "GRAFANA_COOKIE",
        "SLACK_BOT_TOKEN", "SLACK_CHANNEL_ID", "SLACK_THREAD_TS", "SLACK_MESSAGE_TEMPLATE",
        "SCHEDULE_INTERVAL_MINUTES", "TIMEZONE", "VIEWPORT_WIDTH", "VIEWPORT_HEIGHT",
        "PAGE_LOAD_WAIT_SECONDS", "GRAFANA_THEME", "WEB_PORT"
    ]
    updates = {}
    for k in allowed_keys:
        if k in data:
            val = str(data[k]).strip()
            if k in ("SLACK_BOT_TOKEN", "GRAFANA_PASSWORD", "GRAFANA_API_TOKEN") and val.startswith("••••"):
                continue
            updates[k] = val

    if updates:
        Config.save_env_settings(updates)
        bot_log("⚙️ System settings updated & synced to .env.")

    return jsonify({"success": True, "message": "Settings saved successfully"})


@app.route("/api/trigger", methods=["POST"])
def trigger_cycle():
    if BOT_STATE["is_running_cycle"]:
        return jsonify({"success": False, "error": "A capture cycle is already in progress."}), 400

    t = threading.Thread(target=execute_cycle_task, daemon=True)
    t.start()
    return jsonify({"success": True, "message": "Cycle started in background."})


@app.route("/api/test-slack", methods=["POST"])
def test_slack():
    uploader = SlackUploader()
    ok, details = uploader.test_auth()
    if ok:
        user = details.get("user", "Bot")
        team = details.get("team", "Workspace")
        uploader.post_text_message(
            f"🔔 *Slack Test Connection Successful!*\nConnected as `{user}` on `{team}` at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}."
        )
        return jsonify({
            "success": True,
            "message": f"Connected to workspace '{team}' as '{user}'. Sent test ping to channel {Config.SLACK_CHANNEL_ID}."
        })
    return jsonify({"success": False, "error": str(details)}), 400


@app.route("/api/test-capture", methods=["POST"])
def test_capture():
    data = request.get_json() or {}
    url = data.get("url") or Config.GRAFANA_URL
    if not url:
        links = Config.get_links()
        if links:
            url = links[0].get("url")

    if not url:
        return jsonify({"success": False, "error": "No URL provided for capture."}), 400

    bot_log(f"🧪 Running test capture for: {url}")
    try:
        capture_engine = GrafanaCapture()
        preview_filename = f"preview_{int(time.time())}.png"
        preview_path = os.path.join(tempfile.gettempdir(), preview_filename)
        capture_engine.capture_screenshot(target_url=url, output_path=preview_path)

        return jsonify({
            "success": True,
            "preview_url": f"/api/preview/{preview_filename}",
            "message": f"Snapshot captured ({os.path.getsize(preview_path) / 1024:.1f} KB)"
        })
    except Exception as e:
        bot_log(f"❌ Capture preview failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/preview/<filename>", methods=["GET"])
def get_preview(filename):
    safe_name = os.path.basename(filename)
    path = os.path.join(tempfile.gettempdir(), safe_name)
    if os.path.exists(path):
        return send_file(path, mimetype="image/png")
    return "File not found", 404


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
    while True:
        try:
            execute_cycle_task()
        except Exception as e:
            bot_log(f"⚠️ Scheduler error: {e}")

        interval_sec = max(60, Config.SCHEDULE_INTERVAL_MINUTES * 60)
        bot_log(f"⏳ Sleeping for {Config.SCHEDULE_INTERVAL_MINUTES} minute(s)...")
        time.sleep(interval_sec)


def main():
    parser = argparse.ArgumentParser(description="Headless Grafana to Slack Monitoring Bot & Web UI")
    parser.add_argument("--once", action="store_true", help="Capture & upload all active links once, then exit")
    parser.add_argument("--ui-only", action="store_true", help="Launch Web UI only")
    parser.add_argument("--no-web", action="store_true", help="Run background scheduler only (no Web UI)")
    args = parser.parse_args()

    Config.reload()

    if args.once:
        execute_cycle_task()
        return

    if args.ui_only:
        bot_log("🌐 Starting Web Management UI Only...")
        run_web_server()
        return

    if args.no_web:
        bot_log("🤖 Starting Continuous Bot Scheduler Daemon (No Web UI)...")
        scheduler_loop()
        return

    # Default: Start BOTH Scheduler & Web UI
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
