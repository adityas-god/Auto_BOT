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
from urllib.parse import urlparse

import pytz
import requests
from dotenv import load_dotenv
from flask import Flask, render_template_string, request, jsonify, send_file
from playwright.sync_api import sync_playwright

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE_DIR, ".env")
if os.path.isdir(ENV_PATH):
    ENV_PATH = os.path.join(ENV_PATH, "settings.env")

if not os.path.exists(ENV_PATH):
    try:
        example_path = os.path.join(BASE_DIR, ".env.example")
        if os.path.exists(example_path) and os.path.isfile(example_path):
            with open(example_path, "r", encoding="utf-8") as src, open(ENV_PATH, "w", encoding="utf-8") as dst:
                dst.write(src.read())
        else:
            with open(ENV_PATH, "w", encoding="utf-8") as f:
                f.write("# Headless Bot Configuration\n")
    except Exception:
        pass

if os.path.exists(ENV_PATH) and os.path.isfile(ENV_PATH):
    load_dotenv(dotenv_path=ENV_PATH, override=True)


def safe_int(val, default):
    try:
        if val is None:
            return default
        s = str(val).strip()
        return int(s) if s else default
    except (ValueError, TypeError):
        return default


# ==============================================================================
# CONFIGURATION & .ENV MANAGER
# ==============================================================================
class Config:
    GRAFANA_URL = os.getenv("GRAFANA_URL", "").strip()
    GRAFANA_URLS = os.getenv("GRAFANA_URLS", "").strip()
    GRAFANA_API_TOKEN = os.getenv("GRAFANA_API_TOKEN", "").strip()
    GRAFANA_COOKIE = os.getenv("GRAFANA_COOKIE", "").strip()
    GRAFANA_USERNAME = os.getenv("GRAFANA_USERNAME", "").strip()
    GRAFANA_PASSWORD = os.getenv("GRAFANA_PASSWORD", "").strip()
    SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "").strip()
    SLACK_CHANNEL_ID = os.getenv("SLACK_CHANNEL_ID", "").strip()
    SLACK_THREAD_TS = os.getenv("SLACK_THREAD_TS", "").strip()
    SLACK_MESSAGE = (os.getenv("SLACK_MESSAGE") or os.getenv("SLACK_MESSAGE_TEMPLATE") or "📊 *Grafana Snapshot Alert* - {datetime}").strip()
    SCHEDULE_INTERVAL_MINUTES = safe_int(os.getenv("SCHEDULE_INTERVAL_MINUTES"), 30)
    BOT_PAUSED = os.getenv("BOT_PAUSED", "false").strip().lower() == "true"

    TIMEZONE = os.getenv("TIMEZONE", "Asia/Kolkata").strip() or "Asia/Kolkata"
    VIEWPORT_WIDTH = safe_int(os.getenv("VIEWPORT_WIDTH"), 1920)
    VIEWPORT_HEIGHT = safe_int(os.getenv("VIEWPORT_HEIGHT"), 1080)
    PAGE_LOAD_WAIT_SECONDS = safe_int(os.getenv("PAGE_LOAD_WAIT_SECONDS"), 8)
    GRAFANA_THEME = os.getenv("GRAFANA_THEME", "dark").strip() or "dark"
    WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0").strip() or "0.0.0.0"
    WEB_PORT = safe_int(os.getenv("WEB_PORT"), 5000)

    @classmethod
    def reload(cls):
        if os.path.exists(ENV_PATH) and os.path.isfile(ENV_PATH):
            load_dotenv(dotenv_path=ENV_PATH, override=True)
        cls.GRAFANA_URL = os.getenv("GRAFANA_URL", "").strip()
        cls.GRAFANA_URLS = os.getenv("GRAFANA_URLS", "").strip()
        cls.GRAFANA_API_TOKEN = os.getenv("GRAFANA_API_TOKEN", "").strip()
        cls.GRAFANA_COOKIE = os.getenv("GRAFANA_COOKIE", "").strip()
        cls.GRAFANA_USERNAME = os.getenv("GRAFANA_USERNAME", "").strip()
        cls.GRAFANA_PASSWORD = os.getenv("GRAFANA_PASSWORD", "").strip()
        cls.SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "").strip()
        cls.SLACK_CHANNEL_ID = os.getenv("SLACK_CHANNEL_ID", "").strip()
        cls.SLACK_THREAD_TS = os.getenv("SLACK_THREAD_TS", "").strip()
        cls.SLACK_MESSAGE = (os.getenv("SLACK_MESSAGE") or os.getenv("SLACK_MESSAGE_TEMPLATE") or "📊 *Grafana Snapshot Alert* - {datetime}").strip()
        cls.SCHEDULE_INTERVAL_MINUTES = safe_int(os.getenv("SCHEDULE_INTERVAL_MINUTES"), 30)
        cls.BOT_PAUSED = os.getenv("BOT_PAUSED", "false").strip().lower() == "true"
        cls.TIMEZONE = os.getenv("TIMEZONE", "Asia/Kolkata").strip() or "Asia/Kolkata"
        cls.VIEWPORT_WIDTH = safe_int(os.getenv("VIEWPORT_WIDTH"), 1920)
        cls.VIEWPORT_HEIGHT = safe_int(os.getenv("VIEWPORT_HEIGHT"), 1080)
        cls.PAGE_LOAD_WAIT_SECONDS = safe_int(os.getenv("PAGE_LOAD_WAIT_SECONDS"), 8)
        cls.GRAFANA_THEME = os.getenv("GRAFANA_THEME", "dark").strip() or "dark"
        cls.WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0").strip() or "0.0.0.0"
        cls.WEB_PORT = safe_int(os.getenv("WEB_PORT"), 5000)

    @classmethod
    def get_target_urls(cls):
        urls = []
        raw_list = []
        if cls.GRAFANA_URL:
            raw_list.extend(cls.GRAFANA_URL.replace("\n", ",").split(","))
        if cls.GRAFANA_URLS:
            raw_list.extend(cls.GRAFANA_URLS.replace("\n", ",").split(","))
        for u in raw_list:
            clean = u.strip()
            if clean and clean not in urls:
                urls.append(clean)
        return urls

    @classmethod
    def parse_channel_id(cls, raw_input):
        val = raw_input.strip()
        if "/archives/" in val:
            after = val.split("/archives/", 1)[1].split("?")[0].strip("/")
            return after.split("/")[0]
        return val

    @classmethod
    def save_settings(cls, settings_dict):
        # Synchronize SLACK_MESSAGE and SLACK_MESSAGE_TEMPLATE
        if "SLACK_MESSAGE" in settings_dict:
            settings_dict["SLACK_MESSAGE_TEMPLATE"] = settings_dict["SLACK_MESSAGE"]

        lines = []
        if os.path.exists(ENV_PATH) and os.path.isfile(ENV_PATH):
            with open(ENV_PATH, "r", encoding="utf-8") as f:
                lines = f.readlines()

        updated = set()
        new_lines = []
        for line in lines:
            matched = False
            for k, v in settings_dict.items():
                stripped = line.strip()
                if stripped.startswith(f"{k}=") or stripped.startswith(f"{k} ="):
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
    def get_formatted_message(cls, target_url=None, title=None):
        try:
            tz = pytz.timezone(cls.TIMEZONE)
            now = datetime.now(tz)
        except Exception:
            now = datetime.now()

        formatted_datetime = now.strftime("%Y-%m-%d %I:%M:%S %p")
        formatted_date = now.strftime("%Y-%m-%d")
        formatted_time = now.strftime("%I:%M:%S %p")
        active_url = target_url or cls.GRAFANA_URL
        active_title = title or "Grafana Snapshot"

        msg = cls.SLACK_MESSAGE
        msg = msg.replace("\\n", "\n")
        msg = msg.replace("{datetime}", formatted_datetime)
        msg = msg.replace("{date}", formatted_date)
        msg = msg.replace("{time}", formatted_time)
        msg = msg.replace("{grafana_url}", active_url)
        msg = msg.replace("{title}", active_title)
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
        frag = ""
        if "#" in url:
            url, frag = url.split("#", 1)
            frag = f"#{frag}"

        # Non-destructively append kiosk & theme without re-encoding existing variables ($__all, etc.)
        if "/d-solo/" not in url and "kiosk" not in url:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}kiosk=tv"
        if "theme=" not in url:
            theme_val = getattr(self.cfg, "GRAFANA_THEME", "dark") or "dark"
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}theme={theme_val}"
        return f"{url}{frag}"

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

        parsed_origin = urlparse(prepared_url)
        origin_url = f"{parsed_origin.scheme}://{parsed_origin.netloc}"

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
                    "width": int(self.cfg.VIEWPORT_WIDTH),
                    "height": int(self.cfg.VIEWPORT_HEIGHT)
                },
                device_scale_factor=1.0,
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                extra_http_headers=extra_headers,
                ignore_https_errors=True
            )

            # Inject cookies into context with path="/" so all subpaths (/api/, /d/, etc.) receive them
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
                                        entry = {
                                            "name": str(c["name"]).strip(),
                                            "value": str(c["value"]).strip(),
                                            "url": origin_url,
                                            "path": "/"
                                        }
                                        cookies_to_add.append(entry)
                            elif isinstance(parsed_json, dict):
                                for k, v in parsed_json.items():
                                    cookies_to_add.append({
                                        "name": str(k).strip(),
                                        "value": str(v).strip(),
                                        "url": origin_url,
                                        "path": "/"
                                    })
                        except Exception:
                            pass

                    if not cookies_to_add:
                        for part in raw_cookie.split(";"):
                            part = part.strip()
                            if not part:
                                continue
                            if "=" in part:
                                cname, cval = part.split("=", 1)
                                cname = cname.strip()
                                cval = cval.strip().strip('"')
                                cookies_to_add.append({
                                    "name": cname,
                                    "value": cval,
                                    "url": origin_url,
                                    "path": "/"
                                })
                            else:
                                cookies_to_add.append({
                                    "name": "grafana_session",
                                    "value": part.strip().strip('"'),
                                    "url": origin_url,
                                    "path": "/"
                                })

                    if cookies_to_add:
                        context.add_cookies(cookies_to_add)
                        bot_log(f"🍪 Injected {len(cookies_to_add)} session cookie(s) into browser context for {origin_url}")
                except Exception as cookie_err:
                    bot_log(f"⚠️ Warning adding cookies to context: {cookie_err}")

            page = context.new_page()

            try:
                try:
                    response = page.goto(prepared_url, wait_until="domcontentloaded", timeout=30000)
                    if response and response.status >= 400:
                        bot_log(f"⚠️ Server returned HTTP status {response.status}")
                except Exception as nav_err:
                    err_str = str(nav_err)
                    if "ERR_CONNECTION_REFUSED" in err_str:
                        raise ValueError(f"Connection refused at '{prepared_url}'. Verify the service is running and accessible.")
                    if "Timeout" in err_str:
                        raise TimeoutError(f"Connection timed out (30s) reaching '{prepared_url}'. The VM network/firewall cannot reach this server.")
                    raise RuntimeError(f"Navigation failed: {nav_err}")

                # Wait up to 6 seconds for login inputs or dashboard elements
                try:
                    page.wait_for_selector(
                        "input[type='password'], input[name='user'], input[placeholder*='username' i], input[placeholder*='email' i], button:has-text('Log in'), .react-grid-layout, .dashboard-container, [data-testid='dashboard-content']",
                        timeout=6000
                    )
                except Exception:
                    pass

                # Check if we landed on a login screen
                has_pass_input = page.locator("input[type='password'], input[name='password'], input[placeholder*='password' i]").count() > 0
                has_login_btn = page.locator("button:has-text('Log in'), button:has-text('Login'), button:has-text('Sign in'), button[type='submit']").count() > 0
                is_login = "/login" in page.url or (has_pass_input and has_login_btn)

                if is_login:
                    if auth_cookie:
                        bot_log("⚠️ Login screen detected despite session cookie. The session cookie may have expired.")
                    if not user or not pwd:
                        if not auth_cookie and not auth_token:
                            bot_log("⚠️ Grafana login screen detected, but Username / Password / Cookie are not configured in settings!")
                    else:
                        bot_log(f"🔑 Detected login screen. Authenticating as '{user}'...")
                        user_elem = page.locator("input[name='user'], input[id='login-view-username'], input[placeholder*='email' i], input[placeholder*='username' i], input[data-testid*='Username'], input[type='text']").first
                        if user_elem.count() > 0:
                            user_elem.fill(user)
                        time.sleep(0.5)

                        pass_elem = page.locator("input[name='password'], input[id='login-view-password'], input[placeholder*='password' i], input[data-testid*='Password'], input[type='password']").first
                        if pass_elem.count() > 0:
                            pass_elem.fill(pwd)
                        time.sleep(0.5)

                        submit_elem = page.locator("button[type='submit'], button:has-text('Log in'), button:has-text('Login'), button:has-text('Sign in')").first
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

                        # Check if still on login page or need redirect
                        try:
                            curr_url = page.url
                            curr_path = urlparse(curr_url).path
                            prep_path = urlparse(prepared_url).path
                            if "/login" in curr_path:
                                bot_log("⚠️ Still on login screen. Please check if username/password are correct.")
                            elif prep_path and prep_path != "/" and prep_path not in curr_path:
                                bot_log(f"🌐 Redirecting to target dashboard: {prepared_url}")
                                page.goto(prepared_url, wait_until="domcontentloaded", timeout=30000)
                        except Exception:
                            pass

                bot_log("⏳ Waiting for dashboard queries and graphs to finish rendering...")
                try:
                    page.wait_for_load_state("networkidle", timeout=12000)
                except Exception:
                    pass

                # Wait for any loading spinner to disappear
                try:
                    page.wait_for_selector(".panel-loading, .loading-bar", state="hidden", timeout=8000)
                except Exception:
                    pass

                # Wait for actual dashboard panels or content grid to mount
                try:
                    page.wait_for_selector(".react-grid-layout, .panel-content, [data-testid*='panel'], .dashboard-container, .panel-container, [id*='panel'], table", state="visible", timeout=10000)
                except Exception:
                    pass

                time.sleep(self.cfg.PAGE_LOAD_WAIT_SECONDS)

                # Ensure page has settled
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

                # Attempt screenshot: solo panel or full viewport
                shot_taken = False
                if "/d-solo/" in prepared_url or "viewPanel=" in prepared_url:
                    try:
                        panel = page.locator(".panel-container, .react-grid-item, .panel-content, [data-testid*='panel']").first
                        if panel.count() > 0 and panel.is_visible():
                            panel.screenshot(path=output_path)
                            shot_taken = True
                    except Exception as panel_err:
                        bot_log(f"⚠️ Solo panel capture fallback: {panel_err}")

                if not shot_taken:
                    try:
                        page.screenshot(path=output_path, full_page=False)
                    except Exception as shot_err:
                        if "Execution context was destroyed" in str(shot_err) or "navigation" in str(shot_err).lower():
                            bot_log("⚠️ Navigation detected during capture, waiting for page to settle...")
                            time.sleep(3)
                            page.wait_for_load_state("domcontentloaded", timeout=10000)
                            page.screenshot(path=output_path, full_page=False)
                        else:
                            raise shot_err

                if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
                    raise RuntimeError("Screenshot capture failed: output image was not created.")

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
        if not self.token:
            return False, "Slack Bot Token is not configured. Please enter your xoxb-... token."
        url = "https://slack.com/api/auth.test"
        try:
            resp = requests.post(url, headers=self.headers, timeout=15)
            data = resp.json()
            if data.get("ok"):
                return True, data
            err = data.get("error", "Unknown auth error")
            if err == "invalid_auth":
                return False, "Invalid Slack bot token. Ensure it begins with 'xoxb-' and is copied accurately."
            return False, f"Slack Auth Error: {err}"
        except Exception as e:
            return False, str(e)

    def post_text_message(self, message_text):
        if not self.token:
            return False, "Slack Bot Token is missing."
        if not self.channel_id:
            return False, "Slack Channel ID is missing."
        url = "https://slack.com/api/chat.postMessage"
        payload = {"channel": self.channel_id, "text": message_text}
        if Config.SLACK_THREAD_TS:
            payload["thread_ts"] = Config.SLACK_THREAD_TS
        try:
            resp = requests.post(
                url,
                headers={**self.headers, "Content-Type": "application/json; charset=utf-8"},
                data=json.dumps(payload),
                timeout=20
            )
            data = resp.json()
            if data.get("ok"):
                return True, data
            err = data.get("error", "Unknown error")
            if err == "channel_not_found":
                return False, f"Channel '{self.channel_id}' not found. Make sure the channel ID is correct (e.g. C0123456789)."
            if err == "not_in_channel":
                return False, f"The bot is not invited to channel '{self.channel_id}'. Invite the bot using: /invite @botname"
            return False, err
        except Exception as e:
            return False, str(e)

    def upload_screenshot(self, image_path, message_text=None, title=None):
        if not self.token:
            return False, "Slack Bot Token is missing."
        if not self.channel_id:
            return False, "Slack Channel ID is missing."
        if not os.path.exists(image_path):
            return False, f"File not found: {image_path}"

        filename = os.path.basename(image_path)
        file_size = os.path.getsize(image_path)
        comment = message_text or Config.get_formatted_message(title=title)
        file_title = title or f"Grafana Snapshot ({filename})"

        try:
            # Step 1: Request S3 upload URL
            resp1 = requests.get(
                "https://slack.com/api/files.getUploadURLExternal",
                headers=self.headers,
                params={"filename": filename, "length": file_size},
                timeout=20
            )
            data1 = resp1.json()
            if not data1.get("ok"):
                err1 = data1.get("error", "Unknown error")
                if err1 == "missing_scope":
                    return False, "Slack Bot Token is missing the required 'files:write' scope."
                if err1 == "invalid_auth":
                    return False, "Invalid Slack Bot Token. Check that your token begins with 'xoxb-'."
                return False, f"Slack API Step 1 (getUploadURL) failed: {err1}"

            upload_url = data1["upload_url"]
            file_id = data1["file_id"]

            # Step 2: Upload file bytes directly to Slack S3
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

            # Step 3: Complete upload and share to channel
            complete_payload = {
                "files": [{"id": file_id, "title": file_title}],
                "channel_id": self.channel_id,
                "initial_comment": comment
            }
            if Config.SLACK_THREAD_TS:
                complete_payload["thread_ts"] = Config.SLACK_THREAD_TS

            resp3 = requests.post(
                "https://slack.com/api/files.completeUploadExternal",
                headers={**self.headers, "Content-Type": "application/json; charset=utf-8"},
                data=json.dumps(complete_payload),
                timeout=30
            )
            data3 = resp3.json()
            if data3.get("ok"):
                return True, data3

            err3 = data3.get("error", "Unknown error")
            if err3 == "not_in_channel":
                return False, f"Slack upload failed: The bot is not in channel '{self.channel_id}'. Invite it with /invite @botname"
            if err3 == "channel_not_found":
                return False, f"Slack upload failed: Channel '{self.channel_id}' not found. Check the channel ID."
            return False, f"Slack Complete Upload failed: {err3}"
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
_capture_lock = threading.Lock()


def get_current_time_str(fmt="%Y-%m-%d %H:%M:%S"):
    try:
        tz = pytz.timezone(Config.TIMEZONE)
        return datetime.now(tz).strftime(fmt)
    except Exception:
        return datetime.now().strftime(fmt)


def bot_log(message):
    try:
        tz = pytz.timezone(Config.TIMEZONE)
        timestamp = datetime.now(tz).strftime("%H:%M:%S")
    except Exception:
        timestamp = datetime.now().strftime("%H:%M:%S")
    entry = f"[{timestamp}] {message}"
    print(entry, flush=True)
    LOG_BUFFER.append(entry)


def execute_cycle():
    with _state_lock:
        if BOT_STATE["is_running"]:
            bot_log("⚠️ Cycle already running, skipping trigger.")
            return
        BOT_STATE["is_running"] = True
        BOT_STATE["last_status"] = "Capturing..."

    try:
        Config.reload()
        target_urls = Config.get_target_urls()
        if not target_urls:
            bot_log("❌ Grafana Link is empty! Please enter your Grafana URL and click Save.")
            with _state_lock:
                BOT_STATE["last_status"] = "Grafana Link Missing"
            return

        total = len(target_urls)
        bot_log(f"🚀 Starting capture cycle for {total} URL(s)...")

        capture = GrafanaCapture()
        uploader = SlackUploader() if (Config.SLACK_BOT_TOKEN and Config.SLACK_CHANNEL_ID) else None
        uploaded_count = 0
        error_count = 0

        for idx, url in enumerate(target_urls, 1):
            image_path = None
            try:
                bot_log(f"📸 [{idx}/{total}] Capturing: {url}")
                with _capture_lock:
                    image_path = capture.capture_screenshot(target_url=url)
                size_kb = os.path.getsize(image_path) / 1024

                if not uploader:
                    bot_log(f"⚠️ Screenshot [{idx}/{total}] captured ({size_kb:.1f} KB), Slack upload skipped (Token or Channel ID not set).")
                    continue

                bot_log(f"📤 [{idx}/{total}] Uploading snapshot to Slack channel {Config.SLACK_CHANNEL_ID}...")
                formatted_msg = Config.get_formatted_message(target_url=url)
                ok, res = uploader.upload_screenshot(image_path, message_text=formatted_msg)
                if ok:
                    uploaded_count += 1
                    bot_log(f"✅ [{idx}/{total}] Successfully uploaded snapshot to Slack!")
                else:
                    error_count += 1
                    bot_log(f"❌ [{idx}/{total}] Slack upload error: {res}")
            except Exception as item_err:
                error_count += 1
                bot_log(f"💥 [{idx}/{total}] Capture Error: {item_err}")
            finally:
                if image_path and os.path.exists(image_path):
                    try:
                        os.remove(image_path)
                    except Exception:
                        pass

        now_str = get_current_time_str()
        with _state_lock:
            BOT_STATE["last_run_time"] = now_str
            if error_count == 0 and uploaded_count > 0:
                BOT_STATE["last_status"] = f"Success ({uploaded_count} uploaded)"
            elif not uploader:
                BOT_STATE["last_status"] = f"Captured ({total}) - Slack Pending"
            elif error_count > 0 and uploaded_count > 0:
                BOT_STATE["last_status"] = f"Partial ({uploaded_count}/{total} ok, {error_count} failed)"
            else:
                BOT_STATE["last_status"] = "Failed"

    except Exception as e:
        bot_log(f"💥 Cycle Exception: {e}")
        with _state_lock:
            BOT_STATE["last_status"] = f"Error: {e}"
    finally:
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
  <title>Grafana Snapshot Bot — Ops Overview</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@600;700;800&family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
  <style>
    :root {
      --canvas: #f2efe9;
      --card-bg: #ffffff;
      --sidebar-bg: #141416;
      --border-dark: #1a1a1a;
      --border-light: #e4e0d7;
      --primary-orange: #ff5a00;
      --primary-orange-hover: #e04f00;
      --text-dark: #18181b;
      --text-muted: #64748b;
      --text-dim: #71717a;
      --accent-green: #16a34a;
      --accent-blue: #2563eb;
      --accent-amber: #d97706;
      --accent-red: #dc2626;
      --shadow-brutal: 3px 3px 0px var(--border-dark);
      --shadow-sm: 2px 2px 0px var(--border-dark);
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    html, body {
      height: 100vh;
      max-height: 100vh;
      overflow: hidden;
      background: var(--canvas);
      color: var(--text-dark);
      font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
      -webkit-font-smoothing: antialiased;
    }

    /* Overall Layout: Dark Sidebar + Hero Workspace */
    .app-container {
      display: flex;
      height: 100vh;
      width: 100vw;
      overflow: hidden;
    }

    /* Left Sidebar Rail */
    .sidebar {
      width: 68px;
      background: var(--sidebar-bg);
      border-right: 2px solid var(--border-dark);
      display: flex;
      flex-direction: column;
      align-items: center;
      padding: 12px 0;
      flex-shrink: 0;
      z-index: 10;
    }
    .sidebar-logo {
      width: 44px;
      height: 44px;
      background: var(--primary-orange);
      border: 2px solid var(--border-dark);
      box-shadow: var(--shadow-sm);
      border-radius: 4px;
      display: flex;
      align-items: center;
      justify-content: center;
      color: #fff;
      margin-bottom: 20px;
    }
    .nav-list {
      display: flex;
      flex-direction: column;
      gap: 12px;
      width: 100%;
      align-items: center;
      flex: 1;
    }
    .nav-item {
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      gap: 4px;
      width: 52px;
      height: 50px;
      border: 2px solid transparent;
      border-radius: 4px;
      color: #a1a1aa;
      cursor: pointer;
      text-decoration: none;
      font-size: 9px;
      font-weight: 700;
      letter-spacing: 0.05em;
      text-transform: uppercase;
      transition: all 0.15s ease;
    }
    .nav-item.active {
      background: #27272a;
      border-color: var(--primary-orange);
      color: #ffffff;
      box-shadow: 2px 2px 0px var(--primary-orange);
    }
    .nav-item:hover:not(.active) {
      color: #ffffff;
      background: #1f1f23;
    }

    /* Main Workspace */
    .workspace {
      flex: 1;
      display: flex;
      flex-direction: column;
      height: 100vh;
      overflow: hidden;
      min-width: 0;
    }

    /* Top Navigation Bar */
    .topbar {
      height: 52px;
      background: var(--canvas);
      border-bottom: 2px solid var(--border-dark);
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 0 18px;
      flex-shrink: 0;
    }
    .topbar-left {
      display: flex;
      align-items: center;
      gap: 16px;
    }
    .topbar-title {
      font-family: 'Barlow Condensed', sans-serif;
      font-size: 23px;
      font-weight: 800;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      color: var(--text-dark);
    }
    .topbar-tag {
      background: #e4e0d7;
      border: 1px solid var(--border-dark);
      padding: 2px 8px;
      font-size: 11px;
      font-weight: 700;
      text-transform: uppercase;
      border-radius: 2px;
      color: #3f3f46;
    }
    .topbar-right {
      display: flex;
      align-items: center;
      gap: 10px;
    }

    /* Stat Ribbon (Hero Metric Cards) */
    .metrics-ribbon {
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 12px;
      padding: 10px 14px 6px;
      flex-shrink: 0;
    }
    .metric-card {
      background: var(--card-bg);
      border: 2px solid var(--border-dark);
      box-shadow: var(--shadow-sm);
      border-radius: 4px;
      padding: 8px 14px;
      display: flex;
      align-items: center;
      justify-content: space-between;
    }
    .metric-info { display: flex; flex-direction: column; }
    .metric-label {
      font-size: 10.5px;
      font-weight: 700;
      text-transform: uppercase;
      color: var(--text-muted);
      letter-spacing: 0.05em;
    }
    .metric-value {
      font-family: 'Barlow Condensed', sans-serif;
      font-size: 22px;
      font-weight: 800;
      letter-spacing: 0.02em;
      color: var(--text-dark);
      display: flex;
      align-items: center;
      gap: 6px;
    }
    .metric-pill {
      font-family: 'Inter', sans-serif;
      font-size: 10.5px;
      font-weight: 700;
      padding: 3px 8px;
      border: 1.5px solid var(--border-dark);
      border-radius: 3px;
      text-transform: uppercase;
    }
    .pill-green { background: #dcfce7; color: #15803d; }
    .pill-orange { background: #ffedd5; color: #c2410c; }
    .pill-slate { background: #f1f5f9; color: #475569; }

    /* Main Grid: Form Left, Console Right */
    .hero-grid {
      flex: 1;
      min-height: 0;
      display: grid;
      grid-template-columns: 1.15fr 0.85fr;
      gap: 12px;
      padding: 6px 14px 12px;
      overflow: hidden;
    }

    /* Panel Base Style */
    .panel {
      background: var(--card-bg);
      border: 2px solid var(--border-dark);
      box-shadow: var(--shadow-brutal);
      border-radius: 4px;
      display: flex;
      flex-direction: column;
      min-height: 0;
      overflow: hidden;
    }
    .panel-header {
      background: #faf8f5;
      border-bottom: 2px solid var(--border-dark);
      padding: 8px 14px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-shrink: 0;
    }
    .panel-title {
      font-family: 'Barlow Condensed', sans-serif;
      font-size: 16px;
      font-weight: 800;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      color: var(--text-dark);
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .panel-title svg { color: var(--primary-orange); }

    /* Form Body (Zero Page Scroll) */
    .form-panel-body {
      padding: 10px 14px;
      flex: 1;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      overflow: hidden;
    }
    .form-section {
      display: flex;
      flex-direction: column;
      gap: 8px;
    }

    /* Form Fields */
    .field-row {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
    }
    .field-row-3 {
      display: grid;
      grid-template-columns: 1fr 1fr 1fr;
      gap: 10px;
    }
    .form-field {
      display: flex;
      flex-direction: column;
    }
    .form-label {
      font-size: 11px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      color: #27272a;
      margin-bottom: 3px;
      display: flex;
      align-items: center;
      justify-content: space-between;
    }
    .badge-subtle {
      font-size: 9.5px;
      font-weight: 700;
      padding: 1px 5px;
      border-radius: 2px;
      border: 1px solid #d4d4d8;
      background: #f4f4f5;
      color: #52525b;
    }
    .badge-orange-tag {
      background: #fff7ed;
      border-color: #fdba74;
      color: #c2410c;
    }

    /* Normal Inputs (Clean, crisp, standard height) */
    .input-box {
      position: relative;
      display: flex;
      align-items: center;
    }
    .input-field {
      width: 100%;
      height: 32px;
      background: #ffffff;
      border: 2px solid var(--border-dark);
      border-radius: 3px;
      color: var(--text-dark);
      font-family: inherit;
      font-size: 12.5px;
      font-weight: 500;
      padding: 4px 8px;
      outline: none;
      transition: border-color 0.15s, box-shadow 0.15s;
    }
    .input-field:focus {
      border-color: var(--primary-orange);
      box-shadow: 2px 2px 0px var(--primary-orange);
    }
    .input-code {
      font-family: 'JetBrains Mono', monospace;
      font-size: 12px;
    }
    .input-eye-btn {
      position: absolute;
      right: 6px;
      background: transparent;
      border: none;
      color: #71717a;
      cursor: pointer;
      display: flex;
      align-items: center;
      padding: 2px;
    }
    .input-eye-btn:hover { color: var(--text-dark); }

    /* Action Buttons (Neo-brutalist GreyOrange) */
    .form-actions-bar {
      margin-top: 8px;
      padding-top: 8px;
      border-top: 2px dashed var(--border-light);
      display: grid;
      grid-template-columns: 1.4fr 1fr 1fr 1fr;
      gap: 8px;
    }
    .btn {
      height: 36px;
      border: 2px solid var(--border-dark);
      box-shadow: var(--shadow-sm);
      border-radius: 3px;
      font-family: 'Barlow Condensed', sans-serif;
      font-size: 14.5px;
      font-weight: 700;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      cursor: pointer;
      transition: all 0.1s ease;
      user-select: none;
      text-decoration: none;
    }
    .btn:active {
      transform: translate(1px, 1px);
      box-shadow: 1px 1px 0px var(--border-dark);
    }
    .btn-orange {
      background: var(--primary-orange);
      color: #ffffff;
    }
    .btn-orange:hover {
      background: var(--primary-orange-hover);
    }
    .btn-white {
      background: #ffffff;
      color: var(--text-dark);
    }
    .btn-white:hover {
      background: #f4f4f5;
    }
    .btn-dark {
      background: var(--border-dark);
      color: #ffffff;
    }
    .btn-dark:hover {
      background: #27272a;
      border-color: var(--primary-orange);
    }

    /* Terminal Console Panel */
    .console-body {
      flex: 1;
      min-height: 0;
      background: #141416;
      padding: 10px 14px;
      overflow-y: auto;
      font-family: 'JetBrains Mono', monospace;
      font-size: 12px;
      line-height: 1.6;
      color: #a1a1aa;
    }
    .console-body div {
      margin-bottom: 2px;
      word-break: break-all;
    }
    .log-success { color: #4ade80; }
    .log-info { color: #38bdf8; }
    .log-warn { color: #facc15; }
    .log-error { color: #f87171; }

    /* Modal Backdrop */
    .modal-backdrop {
      position: fixed; inset: 0; background: rgba(20, 20, 22, 0.75); backdrop-filter: blur(4px);
      display: none; align-items: center; justify-content: center; z-index: 2000; padding: 20px;
    }
    .modal-box {
      background: #ffffff; border: 2px solid var(--border-dark); box-shadow: 6px 6px 0px var(--border-dark);
      border-radius: 4px; max-width: 900px; width: 100%; max-height: 90vh; display: flex; flex-direction: column; overflow: hidden;
    }
    .modal-header {
      padding: 10px 16px; background: #faf8f5; border-bottom: 2px solid var(--border-dark);
      display: flex; justify-content: space-between; align-items: center;
      font-family: 'Barlow Condensed', sans-serif; font-size: 18px; font-weight: 800; text-transform: uppercase;
    }
    .modal-body { padding: 16px; overflow-y: auto; text-align: center; }
    .modal-body img { max-width: 100%; border: 2px solid var(--border-dark); border-radius: 3px; }

    /* Status dot */
    .status-dot {
      width: 9px; height: 9px; border-radius: 50%;
      background: var(--accent-green);
      display: inline-block;
      border: 1.5px solid var(--border-dark);
    }

    /* Toast */
    .toast {
      position: fixed; bottom: 20px; right: 20px; padding: 10px 16px;
      background: #ffffff; color: var(--text-dark); font-size: 13px; font-weight: 600;
      display: none; align-items: center; gap: 8px; z-index: 3000;
      border: 2px solid var(--border-dark); box-shadow: var(--shadow-brutal);
      border-left: 6px solid var(--primary-orange); border-radius: 3px;
    }
  </style>
</head>
<body>

<div class="app-container">
  <!-- Left Dark Sidebar -->
  <aside class="sidebar">
    <div class="sidebar-logo" title="GreyOrange Headless Bot">
      <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
        <polygon points="12 2 2 7 12 12 22 7 12 2"></polygon>
        <polyline points="2 17 12 22 22 17"></polyline>
        <polyline points="2 12 12 17 22 12"></polyline>
      </svg>
    </div>

    <nav class="nav-list">
      <div class="nav-item active">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7"></rect><rect x="14" y="3" width="7" height="7"></rect><rect x="14" y="14" width="7" height="7"></rect><rect x="3" y="14" width="7" height="7"></rect></svg>
        <span>Ops</span>
      </div>
      <div class="nav-item" onclick="triggerRun()">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="5 3 19 12 5 21 5 3"></polygon></svg>
        <span>Run</span>
      </div>
      <div class="nav-item" onclick="previewSnapshot()">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"></path><circle cx="12" cy="12" r="3"></circle></svg>
        <span>View</span>
      </div>
      <div class="nav-item" onclick="testSlack()">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"></line><polygon points="22 2 15 22 11 13 2 9 22 2"></polygon></svg>
        <span>Slack</span>
      </div>
      <div class="nav-item" onclick="fetchLogs(true)">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"></polyline><polyline points="1 20 1 14 7 14"></polyline><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"></path></svg>
        <span>Logs</span>
      </div>
    </nav>
  </aside>

  <!-- Main Hero Workspace -->
  <main class="workspace">
    <!-- Topbar -->
    <header class="topbar">
      <div class="topbar-left">
        <span class="topbar-title">Grafana Snapshot Bot</span>
        <span class="topbar-tag">Headless Ops Center</span>
      </div>
      <div class="topbar-right">
        <button type="button" class="btn btn-white" id="btn-pause-toggle" onclick="togglePause()" style="height: 32px; font-size: 13px; padding: 0 12px;">
          <svg id="pause-icon" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
            <rect x="6" y="4" width="4" height="16"></rect><rect x="14" y="4" width="4" height="16"></rect>
          </svg>
          <span id="pause-btn-text">Pause Scheduler</span>
        </button>
        <button type="button" class="btn btn-orange" onclick="triggerRun()" style="height: 32px; font-size: 13px; padding: 0 14px;">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
            <polygon points="5 3 19 12 5 21 5 3"></polygon>
          </svg>
          Capture Now
        </button>
      </div>
    </header>

    <!-- Metrics Ribbon (Hero Stat Cards) -->
    <div class="metrics-ribbon">
      <div class="metric-card">
        <div class="metric-info">
          <span class="metric-label">System State</span>
          <span class="metric-value" id="status-text">ACTIVE</span>
        </div>
        <span class="metric-pill pill-green" id="status-pill">ONLINE</span>
      </div>

      <div class="metric-card">
        <div class="metric-info">
          <span class="metric-label">Cadence</span>
          <span class="metric-value" id="metric-interval">30 MINS</span>
        </div>
        <span class="metric-pill pill-slate">AUTO CRON</span>
      </div>

      <div class="metric-card">
        <div class="metric-info">
          <span class="metric-label">Last Run Status</span>
          <span class="metric-value" id="metric-last-status">SUCCESS</span>
        </div>
        <span class="metric-pill pill-orange" id="metric-last-time">READY</span>
      </div>

      <div class="metric-card">
        <div class="metric-info">
          <span class="metric-label">Slack Dispatch</span>
          <span class="metric-value" id="metric-slack-target">CHANNEL</span>
        </div>
        <span class="metric-pill pill-slate">API S3</span>
      </div>
    </div>

    <!-- Main Hero Grid (Form + Console) -->
    <div class="hero-grid">
      <!-- Left Panel: Configuration Form -->
      <section class="panel">
        <div class="panel-header">
          <div class="panel-title">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"></path><circle cx="12" cy="12" r="3"></circle></svg>
            Operations Configuration
          </div>
          <span class="badge-subtle badge-orange-tag">GREYORANGE THEME</span>
        </div>

        <form id="bot-form" class="form-panel-body" onsubmit="event.preventDefault(); saveAll();">
          <div class="form-section">
            <!-- 1. Grafana URL -->
            <div class="form-field">
              <label class="form-label" for="GRAFANA_URL">
                Grafana Target Dashboard Link
              </label>
              <div class="input-box">
                <input type="text" id="GRAFANA_URL" class="input-field input-code" 
                       placeholder="http://172.28.76.144:8088/d/xyz/dashboard?kiosk=tv">
              </div>
            </div>

            <!-- 2. Auth Options: Token & Cookie -->
            <div class="field-row">
              <div class="form-field">
                <label class="form-label" for="GRAFANA_API_TOKEN">
                  Service Account Token
                  <span class="badge-subtle badge-orange-tag">Option 1: API</span>
                </label>
                <div class="input-box">
                  <input type="password" id="GRAFANA_API_TOKEN" class="input-field input-code" 
                         placeholder="glsa_your_token_here">
                  <button type="button" class="input-eye-btn" onclick="toggleVisibility('GRAFANA_API_TOKEN')">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"></path><circle cx="12" cy="12" r="3"></circle></svg>
                  </button>
                </div>
              </div>

              <div class="form-field">
                <label class="form-label" for="GRAFANA_COOKIE">
                  Session Cookie
                  <span class="badge-subtle">Option 2: SSO Bypass</span>
                </label>
                <div class="input-box">
                  <input type="password" id="GRAFANA_COOKIE" class="input-field input-code" 
                         placeholder="grafana_session=abcdef...">
                  <button type="button" class="input-eye-btn" onclick="toggleVisibility('GRAFANA_COOKIE')">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"></path><circle cx="12" cy="12" r="3"></circle></svg>
                  </button>
                </div>
              </div>
            </div>

            <!-- 3. Fallback Username & Password -->
            <div class="field-row">
              <div class="form-field">
                <label class="form-label" for="GRAFANA_USERNAME">Fallback Username</label>
                <input type="text" id="GRAFANA_USERNAME" class="input-field" placeholder="admin or email">
              </div>
              <div class="form-field">
                <label class="form-label" for="GRAFANA_PASSWORD">Fallback Password</label>
                <div class="input-box">
                  <input type="password" id="GRAFANA_PASSWORD" class="input-field" placeholder="••••••••">
                  <button type="button" class="input-eye-btn" onclick="toggleVisibility('GRAFANA_PASSWORD')">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"></path><circle cx="12" cy="12" r="3"></circle></svg>
                  </button>
                </div>
              </div>
            </div>

            <!-- 4. Slack Credentials -->
            <div class="field-row">
              <div class="form-field">
                <label class="form-label" for="SLACK_BOT_TOKEN">Slack Bot OAuth Token</label>
                <div class="input-box">
                  <input type="password" id="SLACK_BOT_TOKEN" class="input-field input-code" 
                         placeholder="xoxb-your-slack-bot-token">
                  <button type="button" class="input-eye-btn" onclick="toggleVisibility('SLACK_BOT_TOKEN')">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"></path><circle cx="12" cy="12" r="3"></circle></svg>
                  </button>
                </div>
              </div>
              <div class="form-field">
                <label class="form-label" for="SLACK_CHANNEL_ID">Slack Target Channel</label>
                <input type="text" id="SLACK_CHANNEL_ID" class="input-field input-code" 
                       placeholder="C0123456789 or channel URL">
              </div>
            </div>

            <!-- 5. Message & Cadence -->
            <div class="field-row" style="grid-template-columns: 2fr 1fr;">
              <div class="form-field">
                <label class="form-label" for="SLACK_MESSAGE">Alert Message Template</label>
                <input type="text" id="SLACK_MESSAGE" class="input-field" 
                       value="Grafana Snapshot Alert - {datetime}">
              </div>
              <div class="form-field">
                <label class="form-label" for="SCHEDULE_INTERVAL_MINUTES">Interval (Mins)</label>
                <input type="number" id="SCHEDULE_INTERVAL_MINUTES" class="input-field" value="30" min="1">
              </div>
            </div>
          </div>

          <!-- Bottom Action Buttons (Fits strictly on screen) -->
          <div class="form-actions-bar">
            <button type="submit" class="btn btn-orange" id="btn-save">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"></path><polyline points="17 21 17 13 7 13 7 21"></polyline><polyline points="7 3 7 8 15 8"></polyline></svg>
              Save Settings
            </button>
            <button type="button" class="btn btn-white" onclick="previewSnapshot()" id="btn-preview">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"></path><circle cx="12" cy="12" r="3"></circle></svg>
              Preview
            </button>
            <button type="button" class="btn btn-white" onclick="testSlack()" id="btn-slack">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"></line><polygon points="22 2 15 22 11 13 2 9 22 2"></polygon></svg>
              Ping Slack
            </button>
            <button type="button" class="btn btn-dark" onclick="triggerRun()" id="btn-run">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--primary-orange)" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polygon points="5 3 19 12 5 21 5 3"></polygon></svg>
              Run Now
            </button>
          </div>
        </form>
      </section>

      <!-- Right Panel: Activity Console -->
      <section class="panel">
        <div class="panel-header">
          <div class="panel-title">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="4 17 10 11 4 5"></polyline><line x1="12" y1="19" x2="20" y2="19"></line></svg>
            Activity Stream Console
          </div>
          <div style="display: flex; gap: 8px;">
            <button type="button" class="btn btn-white" style="height: 24px; font-size: 11px; padding: 0 8px; box-shadow: none;" onclick="clearLogs()">Clear</button>
            <button type="button" class="btn btn-white" style="height: 24px; font-size: 11px; padding: 0 8px; box-shadow: none;" onclick="fetchLogs(true)">Refresh</button>
          </div>
        </div>

        <div class="console-body" id="log-output">
          <div>[System] GreyOrange Operations Center Initialized...</div>
        </div>
      </section>
    </div>
  </main>
</div>

<!-- Preview Modal -->
<div class="modal-backdrop" id="preview-modal" onclick="closeModal()">
  <div class="modal-box" onclick="event.stopPropagation()">
    <div class="modal-header">
      <span>Headless Capture Preview</span>
      <button type="button" class="btn btn-white" style="height: 26px; padding: 0 10px; font-size: 11px;" onclick="closeModal()">Close</button>
    </div>
    <div class="modal-body" id="modal-body"></div>
  </div>
</div>

<div class="toast" id="toast">Notification</div>

<script>
  function showToast(msg, isError = false) {
    const t = document.getElementById("toast");
    t.innerText = msg;
    t.style.borderLeftColor = isError ? "var(--accent-red)" : "var(--primary-orange)";
    t.style.display = "flex";
    setTimeout(() => { t.style.display = "none"; }, 4000);
  }

  function toggleVisibility(id) {
    const el = document.getElementById(id);
    if (!el) return;
    el.type = el.type === "password" ? "text" : "password";
  }

  function clearLogs() {
    document.getElementById("log-output").innerHTML = '<div style="color: #71717a;">[Log cleared]</div>';
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
        if (c.slack_channel_id) {
          document.getElementById("SLACK_CHANNEL_ID").value = c.slack_channel_id;
          document.getElementById("metric-slack-target").innerText = c.slack_channel_id.substring(0, 10);
        }
        if (c.slack_message) document.getElementById("SLACK_MESSAGE").value = c.slack_message;
        if (c.interval) {
          document.getElementById("SCHEDULE_INTERVAL_MINUTES").value = c.interval;
          document.getElementById("metric-interval").innerText = c.interval + " MINS";
        }
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
    const pill = document.getElementById("status-pill");
    const pauseBtn = document.getElementById("btn-pause-toggle");
    const pauseIcon = document.getElementById("pause-icon");
    const pauseText = document.getElementById("pause-btn-text");
    const lastStat = document.getElementById("metric-last-status");
    const lastTime = document.getElementById("metric-last-time");

    if (state.last_status) lastStat.innerText = state.last_status.toUpperCase();
    if (state.last_run_time) lastTime.innerText = state.last_run_time;

    if (state.is_paused) {
      pauseText.innerText = "Resume Scheduler";
      pauseIcon.innerHTML = '<polygon points="5 3 19 12 5 21 5 3"></polygon>';
      text.innerText = "PAUSED";
      pill.innerText = "STANDBY";
      pill.className = "metric-pill pill-orange";
    } else {
      pauseText.innerText = "Pause Scheduler";
      pauseIcon.innerHTML = '<rect x="6" y="4" width="4" height="16"></rect><rect x="14" y="4" width="4" height="16"></rect>';
      if (state.is_running) {
        text.innerText = "CAPTURING";
        pill.innerText = "BUSY";
        pill.className = "metric-pill pill-orange";
      } else {
        text.innerText = "ACTIVE";
        pill.innerText = "ONLINE";
        pill.className = "metric-pill pill-green";
      }
    }
  }

  async function togglePause() {
    try {
      const res = await fetch("/api/toggle-pause", { method: "POST" });
      const data = await res.json();
      if (data.success) {
        showToast(data.is_paused ? "Automatic schedule paused" : "Automatic schedule resumed");
        poll();
      }
    } catch(e) {
      showToast("Failed to toggle scheduler state", true);
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
      showToast("Please enter a valid Grafana Dashboard link.", true);
      return;
    }

    const payload = {
      GRAFANA_URL: grafanaUrl,
      GRAFANA_USERNAME: gUser,
      SLACK_CHANNEL_ID: channel,
      SLACK_MESSAGE: message,
      SCHEDULE_INTERVAL_MINUTES: interval
    };

    if (gToken && !gToken.startsWith("••••")) payload.GRAFANA_API_TOKEN = gToken;
    else if (gToken === "") payload.GRAFANA_API_TOKEN = "";

    if (gCookie && !gCookie.startsWith("••••")) payload.GRAFANA_COOKIE = gCookie;
    else if (gCookie === "") payload.GRAFANA_COOKIE = "";

    if (gPass && !gPass.startsWith("••••")) payload.GRAFANA_PASSWORD = gPass;
    else if (gPass === "") payload.GRAFANA_PASSWORD = "";

    if (token && !token.startsWith("••••")) payload.SLACK_BOT_TOKEN = token;
    else if (token === "") payload.SLACK_BOT_TOKEN = "";

    try {
      const res = await fetch("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      const data = await res.json();
      if (data.success) {
        showToast("Configuration saved and applied.");
        document.getElementById("metric-interval").innerText = interval + " MINS";
        if (channel) document.getElementById("metric-slack-target").innerText = channel.substring(0, 10);
        fetchLogs();
      } else {
        showToast(data.error || "Failed to update configuration", true);
      }
    } catch(e) {
      showToast("Network error while saving settings", true);
    }
  }

  async function previewSnapshot() {
    const url = document.getElementById("GRAFANA_URL").value.trim();
    const gToken = document.getElementById("GRAFANA_API_TOKEN").value.trim();
    const gCookie = document.getElementById("GRAFANA_COOKIE").value.trim();
    const gUser = document.getElementById("GRAFANA_USERNAME").value.trim();
    const gPass = document.getElementById("GRAFANA_PASSWORD").value.trim();

    if (!url) {
      showToast("Please provide a Grafana Dashboard Link before previewing.", true);
      return;
    }

    const modal = document.getElementById("preview-modal");
    const body = document.getElementById("modal-body");
    body.innerHTML = "<div style='padding: 30px;'><p style='font-size:14px; font-weight:600;'>Rendering dashboard in headless Chromium...</p></div>";
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
        body.innerHTML = `<img src="${data.image_url}?t=${Date.now()}"><div style="color:var(--accent-green); font-size:12px; font-weight:700; margin-top:10px;">${data.message}</div>`;
      } else {
        body.innerHTML = `<div style="color: var(--accent-red); padding: 20px; font-weight:600;">Capture Failed: ${data.error}</div>`;
      }
    } catch(e) {
      body.innerHTML = "<div style='color: var(--accent-red); padding: 20px; font-weight:600;'>Network error during capture test.</div>";
    }
    fetchLogs();
  }

  function closeModal() {
    document.getElementById("preview-modal").style.display = "none";
  }

  window.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeModal();
  });

  async function testSlack() {
    const btn = document.getElementById("btn-slack");
    btn.disabled = true;
    const origText = btn.innerText;
    btn.innerText = "Pinging...";
    try {
      const res = await fetch("/api/test-slack", { method: "POST" });
      const data = await res.json();
      if (data.success) showToast(data.message);
      else showToast("Slack Error: " + data.error, true);
    } catch(e) {
      showToast("Slack ping failed", true);
    } finally {
      btn.disabled = false;
      btn.innerText = origText;
      fetchLogs();
    }
  }

  async function triggerRun() {
    const btn = document.getElementById("btn-run");
    btn.disabled = true;
    const origText = btn.innerText;
    btn.innerText = "Running...";
    try {
      const res = await fetch("/api/trigger", { method: "POST" });
      const data = await res.json();
      if (data.success) showToast("Capture task dispatched.");
      else showToast("Run failed: " + data.error, true);
    } catch(e) {
      showToast("Unable to trigger immediate execution", true);
    } finally {
      setTimeout(() => { btn.disabled = false; btn.innerText = origText; }, 2500);
      poll();
    }
  }

  async function fetchLogs(animate = false) {
    try {
      const res = await fetch("/api/logs");
      const data = await res.json();
      const terminal = document.getElementById("log-output");
      terminal.innerHTML = "";
      (data.logs || []).forEach(l => {
        const div = document.createElement("div");
        if (l.includes("[ERROR]") || l.includes("Failed")) {
          div.className = "log-error";
        } else if (l.includes("[SUCCESS]") || l.includes("Uploaded") || l.includes("saved")) {
          div.className = "log-success";
        } else if (l.includes("[WARN]")) {
          div.className = "log-warn";
        } else if (l.includes("[INFO]")) {
          div.className = "log-info";
        }
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
        bot_log(f"[SUCCESS] Settings saved to .env (Token: {bool(Config.GRAFANA_API_TOKEN)}, Cookie: {bool(Config.GRAFANA_COOKIE)})")

    return jsonify({"success": True, "message": "Settings saved successfully"})


def cleanup_old_previews():
    tmp_dir = tempfile.gettempdir()
    now = time.time()
    try:
        for fname in os.listdir(tmp_dir):
            if fname.startswith("preview_") and fname.endswith(".png"):
                full_path = os.path.join(tmp_dir, fname)
                try:
                    if os.path.isfile(full_path) and (now - os.path.getmtime(full_path) > 1800):
                        os.remove(full_path)
                except Exception:
                    pass
    except Exception:
        pass


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

    cleanup_old_previews()

    bot_log(f"[INFO] Live Preview request for: {url}")
    try:
        capture = GrafanaCapture()
        preview_filename = f"preview_{int(time.time() * 1000)}.png"
        preview_path = os.path.join(tempfile.gettempdir(), preview_filename)
        with _capture_lock:
            capture.capture_screenshot(
                target_url=url,
                output_path=preview_path,
                username=username,
                password=password,
                token=token,
                cookie=cookie
            )
        size_kb = os.path.getsize(preview_path) / 1024

        return jsonify({
            "success": True,
            "image_url": f"/api/preview-image/{preview_filename}",
            "message": f"Successfully captured snapshot ({size_kb:.1f} KB)"
        })
    except Exception as e:
        bot_log(f"[ERROR] Preview capture failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/preview-image/<filename>", methods=["GET"])
def get_preview_image(filename):
    safe_name = os.path.basename(filename)
    path = os.path.join(tempfile.gettempdir(), safe_name)
    if os.path.exists(path) and os.path.isfile(path):
        return send_file(path, mimetype="image/png")
    return "Not found", 404


@app.route("/api/toggle-pause", methods=["POST"])
def toggle_pause():
    with _state_lock:
        BOT_STATE["is_paused"] = not BOT_STATE.get("is_paused", False)
        is_paused = BOT_STATE["is_paused"]
        BOT_STATE["last_status"] = "Paused" if is_paused else "Idle"

    Config.save_settings({"BOT_PAUSED": "true" if is_paused else "false"})
    status_str = "PAUSED" if is_paused else "RESUMED"
    bot_log(f"[INFO] Automatic scheduler is now {status_str}")
    return jsonify({"success": True, "is_paused": is_paused})


@app.route("/api/trigger", methods=["POST"])
def trigger_cycle():
    with _state_lock:
        if BOT_STATE["is_running"]:
            return jsonify({"success": False, "error": "A capture cycle is already in progress."}), 400

    t = threading.Thread(target=execute_cycle, daemon=True)
    t.start()
    return jsonify({"success": True, "message": "Capture cycle started."})


@app.route("/api/test-slack", methods=["POST"])
def test_slack():
    uploader = SlackUploader()
    ok, details = uploader.test_auth()
    if not ok:
        return jsonify({"success": False, "error": str(details)}), 400

    user = details.get("user", "Bot")
    team = details.get("team", "Workspace")

    if not Config.SLACK_CHANNEL_ID:
        return jsonify({
            "success": True,
            "message": f"Connected to '{team}' as '{user}'! (Slack Channel ID is empty, so test message was not posted to a channel)."
        })

    post_ok, post_res = uploader.post_text_message(
        f"*Slack Test Ping Successful*\nConnected as `{user}` on `{team}` at {get_current_time_str()}."
    )
    if post_ok:
        return jsonify({
            "success": True,
            "message": f"Connected to '{team}' as '{user}'! Test ping posted to channel {Config.SLACK_CHANNEL_ID}."
        })
    return jsonify({
        "success": False,
        "error": f"Connected as '{user}' on '{team}', but posting to channel failed: {post_res}"
    }), 400


@app.route("/api/logs", methods=["GET"])
def get_logs():
    return jsonify({"logs": list(LOG_BUFFER)})


def run_web_server():
    bot_log(f"[INFO] Web Operations Center running at: http://{Config.WEB_HOST}:{Config.WEB_PORT}")
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
                bot_log(f"[ERROR] Scheduler error: {e}")

            interval_sec = max(60, Config.SCHEDULE_INTERVAL_MINUTES * 60)
            bot_log(f"[INFO] Next scheduled capture in {Config.SCHEDULE_INTERVAL_MINUTES} minute(s)...")

            slept = 0
            while slept < interval_sec:
                with _state_lock:
                    if BOT_STATE.get("is_paused", False):
                        break
                time.sleep(2)
                slept += 2
                interval_sec = max(60, Config.SCHEDULE_INTERVAL_MINUTES * 60)
        else:
            time.sleep(2)


def main():
    parser = argparse.ArgumentParser(description="Headless Grafana to Slack Monitoring Bot")
    parser.add_argument("--once", action="store_true", help="Capture & upload once for all active links, then exit")
    parser.add_argument("--ui-only", action="store_true", help="Start only the Web Management UI without the background scheduler")
    parser.add_argument("--no-web", action="store_true", help="Start only the scheduler loop without starting the web server")
    parser.add_argument("--test-slack", action="store_true", help="Verify Slack bot credentials and post a test ping message")
    parser.add_argument("--test-grafana", action="store_true", help="Take a headless screenshot of active Grafana link(s) and save locally")
    args = parser.parse_args()

    Config.reload()

    if args.test_slack:
        bot_log("[INFO] Testing Slack credentials...")
        uploader = SlackUploader()
        ok, details = uploader.test_auth()
        if not ok:
            bot_log(f"[ERROR] Slack authentication failed: {details}")
            sys.exit(1)
        user = details.get("user", "Bot")
        team = details.get("team", "Workspace")
        bot_log(f"[SUCCESS] Authenticated with Slack as '{user}' on workspace '{team}'!")
        if Config.SLACK_CHANNEL_ID:
            post_ok, post_res = uploader.post_text_message(
                f"*Slack Test Ping Successful*\nCLI test executed at {get_current_time_str()}."
            )
            if post_ok:
                bot_log(f"[SUCCESS] Test message posted to channel {Config.SLACK_CHANNEL_ID} successfully!")
            else:
                bot_log(f"[ERROR] Failed to post message to channel {Config.SLACK_CHANNEL_ID}: {post_res}")
                sys.exit(1)
        else:
            bot_log("[WARN] SLACK_CHANNEL_ID is not configured, skipped posting message.")
        return

    if args.test_grafana:
        bot_log("[INFO] Testing Grafana screenshot capture...")
        urls = Config.get_target_urls()
        if not urls:
            bot_log("[ERROR] No Grafana URL configured! Set GRAFANA_URL in .env or via Web UI.")
            sys.exit(1)
        capture = GrafanaCapture()
        for idx, u in enumerate(urls, 1):
            local_out = f"test_capture_{idx}.png"
            bot_log(f"[INFO] Testing capture for: {u} -> {local_out}")
            try:
                capture.capture_screenshot(target_url=u, output_path=local_out)
                bot_log(f"[SUCCESS] Successfully captured to {local_out} ({os.path.getsize(local_out)/1024:.1f} KB)")
            except Exception as e:
                bot_log(f"[ERROR] Capture failed for {u}: {e}")
                sys.exit(1)
        return

    if args.once:
        execute_cycle()
        return

    if args.no_web:
        bot_log("=" * 65)
        bot_log("[INFO] Grafana -> Slack Daemon Mode (No Web UI)")
        bot_log(f"[INFO] Interval: Every {Config.SCHEDULE_INTERVAL_MINUTES} minute(s)")
        bot_log("=" * 65)
        try:
            scheduler_loop()
        except KeyboardInterrupt:
            bot_log("\n[INFO] Bot stopped.")
        return

    bot_log("=" * 65)
    bot_log("[INFO] Grafana -> Slack Headless Bot & Operations Center")
    bot_log(f"[INFO] Web Operations Center: http://{Config.WEB_HOST}:{Config.WEB_PORT}")
    bot_log(f"[INFO] Interval: Every {Config.SCHEDULE_INTERVAL_MINUTES} minute(s)")
    bot_log("=" * 65)

    if not args.ui_only:
        scheduler_thread = threading.Thread(target=scheduler_loop, daemon=True)
        scheduler_thread.start()

    try:
        run_web_server()
    except KeyboardInterrupt:
        bot_log("\n[INFO] Bot stopped.")


if __name__ == "__main__":
    main()
