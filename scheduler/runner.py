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
from core.utils import safe_int, safe_float, apply_grafana_time_range, format_slack_message, get_current_time_str
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

    lk_last_ts = safe_float(lk.get("last_run_ts"), 0.0)
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
        g_settings = SiteManager.get_global_settings()
        lk_thresh_enabled = bool(lk_threshold.get("enabled", True))
        if not lk_thresh_enabled:
            eval_res = {
                "breached": False,
                "status": "DISABLED",
                "summary": "AI/Threshold detection disabled",
                "reasons": []
            }
        else:
            eval_res = evaluate_threshold(
                extraction,
                threshold_cfg=lk_threshold,
                image_path=lk_image,
                global_settings=g_settings,
                site_id=site_id
            )

        now_str = get_current_time_str(tz_name=tz)
        state_up = {
            "last_status": "Captured" if not lk_thresh_enabled else ("Normal" if eval_res["status"] == "NORMAL" else "Anomaly Detected"),
            "last_run_time": now_str,
            "last_run_ts": now_ts
        }
        if eval_res.get("primary_val") is not None:
            state_up["last_reading"] = eval_res["primary_val"]
        SiteManager.update_site_link(site_id, link_id, state_up, log=False)

        if not lk_thresh_enabled:
            bot_log(f"[{site_name} -> {lk_title}] Snapshot captured ({size_kb:.1f} KB). AI/Threshold detection is OFF (simple post).", site_id=site_id)
        else:
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

        site_shifts = site.get("shifts") or {}
        lk_shifts = lk.get("shifts") or {}
        shifts_cfg = dict(site_shifts)
        shifts_cfg.update({k: v for k, v in lk_shifts.items() if v not in (None, "")})
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
        if eval_res.get("breached") and eval_res.get("ai_evaluation") and eval_res.get("summary"):
            ai_sum = eval_res["summary"].replace("[AI Vision] ", "")
            if "{trigger}" not in template_msg and ai_sum not in user_body:
                user_body += f"\n> *Trigger Details:* {ai_sum}"
        formatted_msg = (tag_mentions + user_body).strip()

        # 1. CHANNEL DESTINATIONS (Always post routine/anomaly snapshots to channel/thread)
        # On normal runs: posts casually with NO tags (tag_mentions is empty).
        # On anomalies: posts with on-duty shift members tagged directly in the thread.
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

        # 2. PRIVATE 1-ON-1 DM ALERTS (DISPATCHED ONLY ON ANOMALY / BREACH)
        # As requested: Routine snapshots stay in the channel/thread without DM interruptions.
        # DMs to on-duty shift members and breach users are triggered ONLY when an anomaly is detected.
        link_send_dm = lk.get("send_dm", True) and shifts_cfg.get("send_dm", True)
        dm_recipients = []

        if eval_res["breached"]:
            for uid in user_dests:
                if uid not in dm_recipients:
                    dm_recipients.append(uid)
            if link_send_dm:
                for uid in all_breach_mentions:
                    if uid not in dm_recipients:
                        dm_recipients.append(uid)
                for uid in active_shift.get("user_ids", []):
                    if uid not in dm_recipients:
                        dm_recipients.append(uid)
        elif not channel_dests:
            # Standalone private DM mode (only when NO channel is configured at all)
            if not lk.get("only_on_breach", False) and not lk_threshold.get("only_alert_on_breach", False):
                dm_recipients = list(user_dests)

        if dm_recipients:
            for uid in dm_recipients:
                bot_log(f"[{site_name} -> {lk_title}] Anomaly detected! Dispatching on-call DM alert to: <@{uid}>...", site_id=site_id)
                dm_ok, dm_res = uploader.send_dm_snapshot(uid, lk_image, message_text=user_body)
                if dm_ok:
                    bot_log(f"[{site_name} -> {lk_title}] Anomaly alert delivered to DM (<@{uid}>)!", site_id=site_id)
                else:
                    bot_log(f"[{site_name} -> {lk_title}] DM alert to <@{uid}> failed: {dm_res}", site_id=site_id)

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


def run_site_bundle_capture_and_alert(site_id, force=False):
    """
    Captures screenshots for all active/due links under a site and dispatches them
    bundled together as a SINGLE Slack message with multiple attachments.
    Same layout as native Slack multi-attachment gallery preview.
    """
    site = SiteManager.get_raw_site(site_id)
    if not site:
        return False, "Site not found"

    site_name = site.get("name", site_id)
    links = SiteManager.get_site_links(site_id, raw=True)
    active_links = [lk for lk in links if lk.get("enabled", True) and (lk.get("url") or "").strip()]

    if not active_links:
        bot_log(f"[{site_name}] No active or configured monitored links with URLs for this site.", site_id=site_id)
        return False, "No active enabled links"

    now_ts = time.time()
    tz = SiteManager.get_timezone()
    site_interval = max(1, safe_int(site.get("interval_minutes"), 30))
    active_intervals = [safe_int(lk.get("interval_minutes"), 0) for lk in active_links if safe_int(lk.get("interval_minutes"), 0) > 0]
    bundle_cadence = min(active_intervals) if active_intervals else site_interval

    # Check if the bundled cycle is due
    site_last_ts = safe_float(site.get("last_run_ts"), 0.0)
    link_ts_list = [safe_float(lk.get("last_run_ts"), 0.0) for lk in active_links if safe_float(lk.get("last_run_ts"), 0.0) > 0]
    if link_ts_list:
        max_link_ts = max(link_ts_list)
        if max_link_ts > site_last_ts:
            site_last_ts = max_link_ts

    if not force and site_last_ts > 0:
        elapsed = now_ts - site_last_ts
        if elapsed < (bundle_cadence * 60) - 5:
            return False, "Cadence interval not due yet"

    links_to_capture = []
    for lk in active_links:
        lk_id = lk.get("id")
        run_key = f"{site_id}_{lk_id}"
        with ACTIVE_LINK_LOCK:
            if run_key in ACTIVE_LINK_RUNS:
                continue
        links_to_capture.append(lk)

    if not links_to_capture:
        return False, "Cadence interval not due yet"

    acquired_links = []
    with ACTIVE_LINK_LOCK:
        for lk in links_to_capture:
            run_key = f"{site_id}_{lk.get('id')}"
            if run_key not in ACTIVE_LINK_RUNS:
                ACTIVE_LINK_RUNS.add(run_key)
                acquired_links.append(lk)

    if not acquired_links:
        return False, "Already running"

    # Immediately lock in site_last_ts so scheduler loop cannot double-trigger while in flight
    SiteManager.record_site_run(
        site_id=site_id,
        last_run_time=get_current_time_str(tz_name=tz),
        last_run_ts=now_ts,
        last_status="Capturing..."
    )

    captured_results = []
    overall_errors = []
    temp_images_to_clean = []

    try:
        for lk in acquired_links:
            lk_id = lk.get("id")
            lk_title = lk.get("title") or "Monitored Link"
            target_url = (lk.get("url") or "").strip()
            time_range = lk.get("time_range")
            if time_range and time_range != "url_default":
                target_url = apply_grafana_time_range(target_url, time_range)

            lk_user, lk_pwd, lk_token = resolve_link_auth(site, lk, target_url=target_url)
            lk_threshold = dict(lk.get("threshold") or {})
            lk_threshold["prev_reading"] = lk.get("last_reading")

            bot_log(f"[{site_name} -> {lk_title}] Initiating capture (bundled mode): {target_url}", site_id=site_id)
            SiteManager.update_site_link(site_id, lk_id, {
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

                temp_images_to_clean.append(lk_image)
                size_kb = os.path.getsize(lk_image) / 1024
                g_settings = SiteManager.get_global_settings()
                lk_thresh_enabled = bool(lk_threshold.get("enabled", True))
                if not lk_thresh_enabled:
                    eval_res = {
                        "breached": False,
                        "status": "DISABLED",
                        "summary": "AI/Threshold detection disabled",
                        "reasons": []
                    }
                else:
                    eval_res = evaluate_threshold(
                        extraction,
                        threshold_cfg=lk_threshold,
                        image_path=lk_image,
                        global_settings=g_settings,
                        site_id=site_id
                    )

                now_str = get_current_time_str(tz_name=tz)
                state_up = {
                    "last_status": "Captured" if not lk_thresh_enabled else ("Normal" if eval_res["status"] == "NORMAL" else "Anomaly Detected"),
                    "last_run_time": now_str,
                    "last_run_ts": now_ts
                }
                if eval_res.get("primary_val") is not None:
                    state_up["last_reading"] = eval_res["primary_val"]
                SiteManager.update_site_link(site_id, lk_id, state_up, log=False)

                if not lk_thresh_enabled:
                    bot_log(f"[{site_name} -> {lk_title}] Snapshot captured ({size_kb:.1f} KB). AI/Threshold detection is OFF (simple post).", site_id=site_id)
                else:
                    bot_log(f"[{site_name} -> {lk_title}] Snapshot captured ({size_kb:.1f} KB). Status: [{eval_res['status']}] - {eval_res['summary']}", site_id=site_id)

                only_on_breach = lk.get("only_on_breach", False) or lk_threshold.get("only_alert_on_breach", False)

                captured_results.append({
                    "link": lk,
                    "image_path": lk_image,
                    "eval_res": eval_res,
                    "title": lk_title,
                    "url": target_url,
                    "threshold": lk_threshold,
                    "breached": eval_res.get("breached", False),
                    "only_on_breach": only_on_breach
                })
            except Exception as cap_err:
                bot_log(f"[{site_name} -> {lk_title}] Capture failed: {cap_err}", site_id=site_id)
                overall_errors.append(f"{lk_title}: {cap_err}")
                SiteManager.update_site_link(site_id, lk_id, {
                    "last_status": f"Error: {cap_err}",
                    "last_run_time": get_current_time_str(tz_name=tz),
                    "last_run_ts": now_ts
                }, log=False)
    finally:
        with ACTIVE_LINK_LOCK:
            for lk in acquired_links:
                ACTIVE_LINK_RUNS.discard(f"{site_id}_{lk.get('id')}")

    if not captured_results:
        now_str = get_current_time_str(tz_name=tz)
        end_ts = time.time()
        fail_status = f"Failed ({', '.join(overall_errors)})" if overall_errors else "Failed (No images captured)"
        SiteManager.record_site_run(
            site_id=site_id,
            last_run_time=now_str,
            last_run_ts=end_ts,
            last_status=fail_status
        )
        return False, fail_status

    g_settings = SiteManager.get_global_settings()
    slack_token = g_settings.get("slack_bot_token")
    if not slack_token:
        bot_log(f"[{site_name}] Slack dispatch skipped (Slack Bot Token not configured).", site_id=site_id)
        _clean_temp_images(temp_images_to_clean)
        now_str = get_current_time_str(tz_name=tz)
        end_ts = time.time()
        SiteManager.record_site_run(
            site_id=site_id,
            last_run_time=now_str,
            last_run_ts=end_ts,
            last_status=f"Captured ({len(captured_results)} links, No Slack Token)"
        )
        for res_item in captured_results:
            lk_obj = res_item.get("link", {})
            if lk_obj.get("id"):
                SiteManager.update_site_link(site_id, lk_obj["id"], {"last_run_ts": end_ts}, log=False)
        return True, "No Slack Bot Token"

    # Filter out links configured with 'only_on_breach' that had no breach
    alertable_items = [it for it in captured_results if not (it["only_on_breach"] and not it["breached"])]
    if not alertable_items:
        bot_log(f"[{site_name}] All clear across {len(captured_results)} link(s). 'Only Alert on Anomaly/Breach' active across all links, skipping Slack alert.", site_id=site_id)
        _clean_temp_images(temp_images_to_clean)
        now_str = get_current_time_str(tz_name=tz)
        end_ts = time.time()
        SiteManager.record_site_run(
            site_id=site_id,
            last_run_time=now_str,
            last_run_ts=end_ts,
            last_status="All Clear (Skipped)"
        )
        for res_item in captured_results:
            lk_obj = res_item.get("link", {})
            if lk_obj.get("id"):
                SiteManager.update_site_link(site_id, lk_obj["id"], {"last_run_ts": end_ts}, log=False)
        return True, "Skipped (All clear)"

    # Group items by Slack destination (channel and thread)
    dest_groups = {}
    for it in alertable_items:
        lk = it["link"]
        raw_dest = (lk.get("slack_channel_id") or "").strip() or (site.get("slack_channel_id") or "").strip()
        thread_ts = str(lk.get("slack_thread_ts") or "").strip() or str(site.get("slack_thread_ts") or "").strip()
        if not raw_dest:
            continue
        dest_groups.setdefault((raw_dest, thread_ts), []).append(it)

    if not dest_groups:
        bot_log(f"[{site_name}] Slack dispatch skipped: No Slack Channel or Member ID configured.", site_id=site_id)
        _clean_temp_images(temp_images_to_clean)
        now_str = get_current_time_str(tz_name=tz)
        end_ts = time.time()
        SiteManager.record_site_run(
            site_id=site_id,
            last_run_time=now_str,
            last_run_ts=end_ts,
            last_status=f"Captured ({len(captured_results)} links, No Destination)"
        )
        for res_item in captured_results:
            lk_obj = res_item.get("link", {})
            if lk_obj.get("id"):
                SiteManager.update_site_link(site_id, lk_obj["id"], {"last_run_ts": end_ts}, log=False)
        return True, "No destination"

    for (raw_dest, thread_ts), group_items in dest_groups.items():
        raw_tokens = [re.sub(r"[<@>#\s]", "", t).strip() for t in re.split(r"[,;\s]+", raw_dest) if t.strip()]
        channel_dests = [t for t in raw_tokens if t.startswith("C")]
        user_dests = [t for t in raw_tokens if t.startswith(("U", "W"))]
        other_dests = [t for t in raw_tokens if not t.startswith("C") and not t.startswith(("U", "W"))]
        channel_dests.extend(other_dests)

        default_chan = channel_dests[0] if channel_dests else (user_dests[0] if user_dests else None)
        if not default_chan:
            continue

        uploader = SlackUploader(token=slack_token, channel_id=default_chan, thread_ts=thread_ts)

        # Build list of items for multi-attachment upload
        image_items = [{"path": it["image_path"], "title": f"{site_name} — {it['title']}"} for it in group_items]

        site_shifts = site.get("shifts") or {}
        active_shift = get_active_shift(site_shifts, tz) if site_shifts else {"name": "Default", "user_ids": []}

        all_breach_mentions = []
        any_breach = any(it["breached"] for it in group_items)
        if any_breach:
            for it in group_items:
                if it["breached"]:
                    raw_b = it["threshold"].get("breach_users", "")
                    b_ids = [u.strip().strip("<>@#") for u in raw_b.replace(",", " ").split() if u.strip()]
                    for u in b_ids:
                        if u not in all_breach_mentions:
                            all_breach_mentions.append(u)
            if site_shifts.get("tag_channel", True) and active_shift.get("user_ids"):
                for u in active_shift["user_ids"]:
                    if u not in all_breach_mentions:
                        all_breach_mentions.append(u)

        tag_mentions = (" ".join([f"<@{u}>" for u in all_breach_mentions]) + "\n") if (all_breach_mentions and channel_dests) else ""

        # Determine message template: check links in this group first, then site fallback
        link_template = ""
        for it in group_items:
            lk_msg = (it.get("link", {}).get("slack_message") or "").strip()
            if lk_msg and lk_msg != "*Grafana Snapshot Alert* - {datetime}":
                link_template = lk_msg
                break
        if not link_template and group_items:
            link_template = (group_items[0].get("link", {}).get("slack_message") or "").strip()

        template_msg = link_template or site.get("slack_message") or "*Grafana Snapshot Alert* - {datetime}"
        user_body = format_slack_message(
            template_msg,
            title=site_name,
            tz_name=tz,
            trigger="; ".join([it["eval_res"].get("summary", "") for it in group_items if it.get("breached")]),
            shift=active_shift.get("name", "")
        )

        # Only append anomaly summaries if an actual breach occurred
        # If AI/Anomaly detection is OFF or all dashboards are normal, simply post the message cleanly
        if any_breach and "{trigger}" not in template_msg:
            tab_summaries = []
            for it in group_items:
                if it.get("breached"):
                    sm = it["eval_res"].get("summary", "")
                    tab_summaries.append(f"• *{it['title']}*: ⚠️ Anomaly ({sm})" if sm else f"• *{it['title']}*: ⚠️ Anomaly")
            if tab_summaries:
                user_body += "\n" + "\n".join(tab_summaries)

        formatted_msg = (tag_mentions + user_body).strip()

        # 1. Post to channel(s)
        if channel_dests:
            for c_chan in channel_dests:
                thread_info = f" (thread: {thread_ts})" if thread_ts else ""
                bot_log(f"[{site_name}] Uploading {len(image_items)} bundled snapshot(s) in a single message to Slack channel '{c_chan}'{thread_info}...", site_id=site_id)
                ok, res = uploader.upload_multiple_screenshots(
                    image_items,
                    message_text=formatted_msg,
                    target_channel_id=c_chan,
                    thread_ts=thread_ts
                )
                if ok:
                    bot_log(f"[{site_name}] Successfully delivered {len(image_items)} snapshot(s) in 1 message to Slack ({c_chan})!", site_id=site_id)
                else:
                    bot_log(f"[{site_name}] Bundled Slack upload to '{c_chan}' failed: {res}", site_id=site_id)

        # 2. Dispatch DM alerts if breach
        dm_recipients = []
        if any_breach:
            for uid in user_dests:
                if uid not in dm_recipients:
                    dm_recipients.append(uid)
            if site_shifts.get("send_dm", True):
                for uid in all_breach_mentions:
                    if uid not in dm_recipients:
                        dm_recipients.append(uid)
                for uid in active_shift.get("user_ids", []):
                    if uid not in dm_recipients:
                        dm_recipients.append(uid)
        elif not channel_dests:
            dm_recipients = list(user_dests)

        if dm_recipients:
            for uid in dm_recipients:
                if any_breach:
                    bot_log(f"[{site_name}] Anomaly detected! Dispatching {len(image_items)} bundled snapshot(s) to DM: <@{uid}>...", site_id=site_id)
                else:
                    bot_log(f"[{site_name}] Dispatching {len(image_items)} bundled snapshot(s) to DM: <@{uid}>...", site_id=site_id)
                dm_ok, dm_res = uploader.send_dm_multiple_snapshots(uid, image_items, message_text=user_body)
                if dm_ok:
                    bot_log(f"[{site_name}] Bundled snapshots delivered to DM (<@{uid}>)!", site_id=site_id)
                else:
                    bot_log(f"[{site_name}] Bundled DM to <@{uid}> failed: {dm_res}", site_id=site_id)

    _clean_temp_images(temp_images_to_clean)

    now_str = get_current_time_str(tz_name=tz)
    end_ts = time.time()
    overall_status = f"Completed ({len(captured_results)} links bundled)" if not overall_errors else f"Warnings ({len(overall_errors)} links failed)"
    SiteManager.record_site_run(
        site_id=site_id,
        last_run_time=now_str,
        last_run_ts=end_ts,
        last_status=overall_status
    )
    for res_item in captured_results:
        lk_obj = res_item.get("link", {})
        if lk_obj.get("id"):
            SiteManager.update_site_link(site_id, lk_obj["id"], {"last_run_ts": end_ts}, log=False)
    return True, overall_status


def _clean_temp_images(paths):
    for p in paths:
        if p and os.path.exists(p):
            try:
                os.remove(p)
            except Exception:
                pass

