"""
Link Execution & Alert Runner for GreyOrange OpsBot.
Orchestrates single-link snapshot captures, anomaly evaluations, and targeted Slack deliveries.
Guarantees strict destination isolation and link-level concurrency safety.
"""

import os
import re
import time

from core.config import ACTIVE_LINK_LOCK, ACTIVE_LINK_RUNS, GLOBAL_CAPTURE_SEMAPHORE
from core.database import SiteManager
from core.auth import resolve_link_auth
from core.logger import bot_log
from core.shifts import get_active_shift
from core.utils import safe_int, apply_grafana_time_range, format_slack_message, get_current_time_str
from engines.grafana_capture import GrafanaCapture
from engines.anomaly_engine import evaluate_threshold
from engines.slack_notifier import SlackUploader


def run_link_capture_and_alert(site_id, link_id, force=False, caller_acquired_lock=False):
    """
    Executes isolated headless capture, anomaly evaluation, and targeted Slack delivery
    for a SINGLE monitored link tab.
    Strict destination isolation:
    - Never leaks messages across sites or tabs.
    - If target destination is a Slack Member ID (starts with U or W, e.g. U0BJ1CP982V),
      delivers ONLY to that 1-on-1 DM. Under direct DM delivery, NO secondary DMs are dispatched.
    - If target destination is a Slack Channel (starts with C), uploads snapshot to that channel,
      and sends secondary DMs ONLY to users explicitly designated in this link's own configuration.
    """
    site = SiteManager.get_raw_site(site_id)
    if not site:
        return False, "Site not found"

    site_name = site.get("name", site_id)
    links = SiteManager.get_site_links(site_id, raw=True)
    lk = next((l for l in links if l.get("id") == link_id), None)
    if not lk:
        return False, f"Link '{link_id}' not found"

    target_url = (lk.get("url") or "").strip()
    if not target_url:
        return False, "No URL configured for this link"

    if not force and not lk.get("enabled", True):
        return False, "Link is disabled"

    now_ts = time.time()
    tz = SiteManager.get_timezone()
    lk_interval = safe_int(lk.get("interval_minutes"), 0)
    if lk_interval <= 0:
        lk_interval = max(1, safe_int(site.get("interval_minutes"), 30))

    lk_last_ts = lk.get("last_run_ts") or 0
    if not force and lk_last_ts > 0:
        elapsed = now_ts - lk_last_ts
        if elapsed < (lk_interval * 60) - 5:
            return False, "Cadence interval not due yet"

    lk_title = lk.get("title") or "Monitored Link"

    # Strict concurrency guard: Ensure this exact link cannot run multiple times concurrently
    run_key = f"{site_id}_{link_id}"
    with ACTIVE_LINK_LOCK:
        if not caller_acquired_lock:
            if run_key in ACTIVE_LINK_RUNS:
                bot_log(f"[{site_name} -> {lk_title}] Capture already running for this link tab, skipping duplicate run.", site_id=site_id)
                return False, "Already running"
            ACTIVE_LINK_RUNS.add(run_key)

    time_range = lk.get("time_range")
    if time_range and time_range != "url_default":
        target_url = apply_grafana_time_range(target_url, time_range)

    lk_user, lk_pwd, lk_token = resolve_link_auth(site, lk, target_url=target_url)
    lk_threshold = dict(lk.get("threshold") or {})
    lk_threshold["prev_reading"] = lk.get("last_reading")

    bot_log(f"[{site_name} -> {lk_title}] Initiating capture ({lk_interval}m cadence): {target_url}", site_id=site_id)
    # Immediately record last_run_ts to prevent any scheduler or background thread from re-triggering
    SiteManager.update_site_link(site_id, link_id, {
        "last_status": "Capturing...",
        "last_run_ts": now_ts
    }, log=False)

    lk_image = None
    try:
        with GLOBAL_CAPTURE_SEMAPHORE:
            capture = GrafanaCapture(site_dict=site)
            lk_image, extraction = capture.capture_screenshot(
                target_url=target_url,
                output_path=None,
                username=lk_user,
                password=lk_pwd,
                token=lk_token,
                return_extracted=True
            )

        if not lk_image or not os.path.exists(lk_image):
            raise RuntimeError("Capture returned no output image file")

        size_kb = os.path.getsize(lk_image) / 1024
        eval_res = evaluate_threshold(extraction, lk_threshold)

        now_str = get_current_time_str(tz_name=tz)
        state_up = {
            "last_status": "Normal" if eval_res["status"] == "NORMAL" else "Anomaly Detected",
            "last_run_time": now_str,
            "last_run_ts": now_ts
        }
        if eval_res.get("primary_val") is not None:
            state_up["last_reading"] = eval_res["primary_val"]
        SiteManager.update_site_link(site_id, link_id, state_up, log=False)

        bot_log(f"[{site_name} -> {lk_title}] Snapshot captured ({size_kb:.1f} KB). Status: [{eval_res['status']}] - {eval_res['summary']}", site_id=site_id)

        only_on_breach = lk.get("only_on_breach", False) or lk_threshold.get("only_alert_on_breach", False)
        if only_on_breach and not eval_res["breached"]:
            bot_log(f"[{site_name} -> {lk_title}] All clear. 'Only Alert on Anomaly/Breach' active, skipping Slack alert.", site_id=site_id)
            SiteManager.update_site_link(site_id, link_id, {
                "last_status": "Normal (Skipped)",
                "last_run_time": now_str,
                "last_run_ts": now_ts
            }, log=False)
            return True, "Skipped (Normal)"

        g_settings = SiteManager.get_global_settings()
        slack_token = g_settings.get("slack_bot_token")
        if not slack_token:
            bot_log(f"[{site_name} -> {lk_title}] Slack dispatch skipped (Slack Bot Token not configured).", site_id=site_id)
            return True, "No Slack Bot Token"

        # Dedicated destination resolution (Priority: link slack_channel_id)
        raw_dest = (lk.get("slack_channel_id") or "").strip()
        if not raw_dest:
            raw_dest = (site.get("slack_channel_id") or "").strip()

        raw_tokens = [re.sub(r"[<@>#\s]", "", t).strip() for t in re.split(r"[,;\s]+", raw_dest) if t.strip()]
        channel_dests = [t for t in raw_tokens if t.startswith("C")]
        user_dests = [t for t in raw_tokens if t.startswith(("U", "W"))]
        other_dests = [t for t in raw_tokens if not t.startswith("C") and not t.startswith(("U", "W"))]
        channel_dests.extend(other_dests)

        if not channel_dests and not user_dests:
            bot_log(f"[{site_name} -> {lk_title}] Slack dispatch skipped: No Slack Channel or Member ID configured.", site_id=site_id)
            return True, "No destination"

        # Dedicated destination & thread resolution (Priority: link-level, Fallback: site-level)
        thread_ts = str(lk.get("slack_thread_ts") or "").strip() or str(site.get("slack_thread_ts") or "").strip()
        default_chan = channel_dests[0] if channel_dests else user_dests[0]
        uploader = SlackUploader(token=slack_token, channel_id=default_chan, thread_ts=thread_ts)

        shifts_cfg = lk.get("shifts") or {}
        active_shift = get_active_shift(shifts_cfg, tz) if shifts_cfg else {"name": "Default", "user_ids": []}

        tag_mentions = ""
        all_breach_mentions = []
        if eval_res["breached"]:
            raw_breach_users = lk_threshold.get("breach_users", "")
            breach_ids = [u.strip().strip("<>@#") for u in raw_breach_users.replace(",", " ").split() if u.strip()]
            for u in breach_ids:
                if u not in all_breach_mentions:
                    all_breach_mentions.append(u)
            if shifts_cfg.get("tag_channel", True) and active_shift.get("user_ids"):
                for u in active_shift["user_ids"]:
                    if u not in all_breach_mentions:
                        all_breach_mentions.append(u)
            if all_breach_mentions and channel_dests:
                tag_mentions = " ".join([f"<@{u}>" for u in all_breach_mentions]) + "\n"

        template_msg = lk.get("slack_message") or site.get("slack_message") or "*Grafana Snapshot Alert* - {datetime}"
        user_body = format_slack_message(
            template_msg,
            target_url=target_url,
            title=f"{site_name} — {lk_title}",
            tz_name=tz,
            trigger=eval_res.get("summary", ""),
            shift=active_shift.get("name", "")
        )
        formatted_msg = (tag_mentions + user_body).strip()

        # 1. DIRECT 1-ON-1 DM DESTINATIONS (e.g. U0BJ1CP982V, U04RR0S1389)
        # Delivers individual 1-on-1 private messages to each designated member
        for uid in user_dests:
            bot_log(f"[{site_name} -> {lk_title}] Delivering snapshot directly to Slack 1-on-1 DM (<@{uid}>)...", site_id=site_id)
            ok, res = uploader.upload_screenshot(
                lk_image,
                message_text=formatted_msg,
                title=f"{site_name} - {lk_title}",
                target_channel_id=uid,
                skip_thread=True
            )
            if ok:
                bot_log(f"[{site_name} -> {lk_title}] Snapshot successfully delivered to DM (<@{uid}>)!", site_id=site_id)
            else:
                bot_log(f"[{site_name} -> {lk_title}] Direct DM upload to <@{uid}> failed: {res}", site_id=site_id)

        # 2. CHANNEL DESTINATIONS (Starts with C or public channel)
        if channel_dests and lk.get("send_channel", True):
            for c_chan in channel_dests:
                thread_info = f" (thread: {thread_ts})" if thread_ts else ""
                bot_log(f"[{site_name} -> {lk_title}] Uploading snapshot to Slack channel '{c_chan}'{thread_info}...", site_id=site_id)
                ok, res = uploader.upload_screenshot(
                    lk_image,
                    message_text=formatted_msg,
                    title=f"{site_name} - {lk_title}",
                    target_channel_id=c_chan,
                    thread_ts=thread_ts
                )
                if ok:
                    bot_log(f"[{site_name} -> {lk_title}] Successfully delivered to Slack channel ({c_chan})!", site_id=site_id)
                else:
                    bot_log(f"[{site_name} -> {lk_title}] Slack channel upload to '{c_chan}' failed: {res}", site_id=site_id)

        # 3. SECONDARY ON-DUTY SHIFT DM DISPATCH
        link_send_dm = lk.get("send_dm", True) and shifts_cfg.get("send_dm", True)
        if link_send_dm and channel_dests:
            target_uids = []
            if eval_res["breached"]:
                target_uids = list(all_breach_mentions)
            elif not lk.get("only_on_breach", False) and not lk_threshold.get("only_alert_on_breach", False):
                target_uids = list(active_shift.get("user_ids", []))

            for uid in target_uids:
                if uid in user_dests:
                    continue  # Already received direct DM above
                bot_log(f"[{site_name} -> {lk_title}] Dispatching secondary on-call DM alert to: <@{uid}>...", site_id=site_id)
                dm_ok, dm_res = uploader.send_dm_snapshot(uid, lk_image, message_text=user_body)
                if dm_ok:
                    bot_log(f"[{site_name} -> {lk_title}] DM delivered to <@{uid}>!", site_id=site_id)
                else:
                    bot_log(f"[{site_name} -> {lk_title}] DM to <@{uid}> failed: {dm_res}", site_id=site_id)

        SiteManager.update_site_link(site_id, link_id, {
            "last_status": "Success" if eval_res["status"] == "NORMAL" else "Anomaly Detected",
            "last_run_time": get_current_time_str(tz_name=tz),
            "last_run_ts": now_ts
        }, log=False)
        return True, "Success"

    except Exception as exc:
        bot_log(f"[{site_name} -> {lk_title}] Capture & alert error: {exc}", site_id=site_id)
        SiteManager.update_site_link(site_id, link_id, {
            "last_status": f"Error: {exc}",
            "last_run_time": get_current_time_str(tz_name=tz)
        }, log=False)
        return False, str(exc)
    finally:
        with ACTIVE_LINK_LOCK:
            ACTIVE_LINK_RUNS.discard(run_key)
        if lk_image and os.path.exists(lk_image):
            try:
                os.remove(lk_image)
            except Exception:
                pass
