"""
Slack Alerting & 3-Step S3 Upload Engine for GreyOrange OpsBot.
Provides:
- 3-step Slack S3 upload for screenshots (files.getUploadURLExternal -> direct S3 -> files.completeUploadExternal)
- Direct 1-on-1 private DM resolution via conversations.open
- Channel posting with threads and custom formatting
- OAuth scope diagnostics (chat:write, files:write, im:write)
"""

import os
import re
import json
import requests

from core.database import SiteManager


def clean_slack_thread_ts(val):
    if not val:
        return None
    val = str(val).strip()
    if not val:
        return None
    if "thread_ts=" in val:
        val = val.split("thread_ts=", 1)[1].split("&")[0].strip()
    if "/thread/" in val:
        after = val.split("/thread/", 1)[1].split("?")[0].strip("/")
        if "-" in after:
            val = after.split("-", 1)[1]
    if "/archives/" in val and "/p" in val:
        p_part = val.split("/archives/", 1)[1].split("?")[0].strip("/").split("/")[-1]
        if p_part.startswith("p") and len(p_part) > 7:
            digits = p_part[1:]
            return f"{digits[:-6]}.{digits[-6:]}"
    if val.startswith("p") and val[1:].replace(".", "").isdigit() and len(val) > 7:
        digits = val[1:]
        if "." in digits:
            return digits
        return f"{digits[:-6]}.{digits[-6:]}"
    return val


class SlackUploader:
    def __init__(self, token=None, channel_id=None, thread_ts=None):
        self.token = token or SiteManager.get_global_settings().get("slack_bot_token")
        self.channel_id = channel_id
        self.thread_ts = clean_slack_thread_ts(thread_ts)
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
                return False, "Invalid Slack bot token. Ensure it begins with 'xoxb-'."
            return False, f"Slack Auth Error: {err}"
        except Exception as e:
            return False, str(e)

    def post_text_message(self, message_text, target_channel=None, thread_ts=None):
        if not self.token:
            return False, "Slack Bot Token is missing."
        dest = target_channel or self.channel_id
        if not dest:
            return False, "Slack Channel ID is missing."

        # If dest is a Slack User Member ID (e.g. U... or W...), resolve to 1-on-1 DM channel
        clean_user = re.sub(r"[<@>#\s]", "", str(dest).strip())
        skip_thread = False
        if clean_user.startswith(("U", "W")):
            dm_chan, dm_err = self.open_dm_channel(clean_user)
            if dm_chan:
                dest = dm_chan
                skip_thread = True
            else:
                return False, f"Could not open Slack DM channel with user '{clean_user}': {dm_err}"

        url = "https://slack.com/api/chat.postMessage"
        payload = {"channel": dest, "text": message_text}
        effective_thread = thread_ts or (self.thread_ts if not skip_thread else None)
        if effective_thread and not skip_thread:
            clean_thread = clean_slack_thread_ts(effective_thread)
            if clean_thread:
                payload["thread_ts"] = clean_thread
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
                return False, f"Channel '{dest}' not found. If private, invite the bot with '/invite @botname'."
            if err == "not_in_channel":
                return False, f"The bot is not invited to channel '{dest}'. Type /invite @botname in the channel."
            return False, err
        except Exception as e:
            return False, str(e)

    def upload_screenshot(self, image_path, message_text=None, title=None, target_channel_id=None, thread_ts=None, skip_thread=False):
        if not self.token:
            return False, "Slack Bot Token is missing."
        dest_channel = target_channel_id or self.channel_id
        if not dest_channel:
            return False, "Slack Channel ID is missing."
        if not os.path.exists(image_path):
            return False, f"File not found: {image_path}"

        # If dest_channel is a Slack User Member ID (e.g. U... or W...), resolve to 1-on-1 DM channel
        clean_dest = re.sub(r"[<@>#\s]", "", str(dest_channel).strip())
        if clean_dest.startswith(("U", "W")):
            dm_chan, dm_err = self.open_dm_channel(clean_dest)
            if not dm_chan:
                return False, f"Could not open Slack DM channel with user '{clean_dest}': {dm_err}"
            dest_channel = dm_chan
            skip_thread = True

        filename = os.path.basename(image_path)
        file_size = os.path.getsize(image_path)
        comment = message_text or "*Grafana Snapshot Alert*"
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
                return False, f"Slack getUploadURL failed: {err1}"

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

            # Step 3: Complete upload and share to channel or DM
            complete_payload = {
                "files": [{"id": file_id, "title": file_title}],
                "channel_id": dest_channel,
                "initial_comment": comment
            }
            effective_thread = thread_ts or (self.thread_ts if not skip_thread else None)
            if effective_thread and not skip_thread:
                clean_thread = clean_slack_thread_ts(effective_thread)
                if clean_thread:
                    complete_payload["thread_ts"] = clean_thread

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
                return False, f"Slack upload failed: The bot is not in channel '{dest_channel}'. Invite with /invite @botname"
            if err3 == "channel_not_found":
                return False, f"Slack upload failed: Channel '{dest_channel}' not found."
            return False, f"Slack Complete Upload failed: {err3}"
        except Exception as e:
            return False, str(e)

    def open_dm_channel(self, user_id):
        raw = str(user_id or "").strip()
        clean_user = re.sub(r"[<@>#\s]", "", raw)
        if "slack.com" in clean_user:
            clean_user = clean_user.rstrip("/").split("?")[0].split("/")[-1]
            clean_user = re.sub(r"[<@>#\s]", "", clean_user)

        # Failsafe: if multiple IDs were provided, take only the single user ID
        if "," in clean_user:
            clean_user = clean_user.split(",")[0].strip()

        if not clean_user or not clean_user.startswith(("U", "W")):
            return None, f"Invalid Slack User ID format: '{clean_user}'. Slack Member IDs start with 'U' or 'W'."

        url = "https://slack.com/api/conversations.open"
        try:
            res = requests.post(url, headers=self.headers, json={"users": clean_user}, timeout=15)
            data = res.json()
            if data.get("ok"):
                return data["channel"]["id"], None
            err = data.get("error", "Unknown error opening DM")
            if err == "missing_scope":
                needed = data.get("needed", "im:write")
                provided = data.get("provided", "")
                prov_str = f" (Current token scopes: {provided})" if provided else ""
                return None, f"Slack Bot Token is missing the '{needed}' OAuth scope{prov_str}. Please add '{needed}' under 'Bot Token Scopes' at https://api.slack.com/apps and reinstall the app."
            return None, f"Slack conversations.open failed: {err}"
        except Exception as e:
            return None, str(e)

    def send_dm_snapshot(self, user_id, image_path, message_text=None):
        dm_chan, err = self.open_dm_channel(user_id)
        if not dm_chan:
            return False, f"Could not open DM channel with {user_id}: {err}"
        return self.upload_screenshot(
            image_path=image_path,
            message_text=message_text,
            target_channel_id=dm_chan,
            skip_thread=True
        )

    def send_dm_text(self, user_id, message_text):
        dm_chan, err = self.open_dm_channel(user_id)
        if not dm_chan:
            return False, f"Could not open DM channel with {user_id}: {err}"
        url = "https://slack.com/api/chat.postMessage"
        payload = {"channel": dm_chan, "text": message_text}
        try:
            resp = requests.post(
                url,
                headers={**self.headers, "Content-Type": "application/json; charset=utf-8"},
                data=json.dumps(payload),
                timeout=15
            )
            data = resp.json()
            if data.get("ok"):
                return True, data
            return False, data.get("error", "Unknown DM error")
        except Exception as e:
            return False, str(e)
