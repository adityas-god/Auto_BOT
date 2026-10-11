"""
OpsBot Web Operations Center - Flask Server & REST API Endpoints
Provides full REST management for multi-site monitoring, link management,
shift schedules, anomaly alerts, live Playwright previews, and log streaming.
"""

import os
import sys

# Ensure repository root is on sys.path for direct execution (e.g. `python web/app.py`)
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import json
import time
import threading
import tempfile
from datetime import datetime
from flask import Flask, jsonify, request, send_file

from core.config import BASE_DIR, GLOBAL_CAPTURE_SEMAPHORE, ACTIVE_LINK_LOCK, ACTIVE_LINK_RUNS
from core.logger import bot_log, LOG_BUFFER
from core.utils import safe_int, get_current_time_str, apply_grafana_time_range
from core.shifts import get_active_shift
from core.auth import resolve_link_auth
from core.database import SiteManager
from engines.grafana_capture import GrafanaCapture
from engines.anomaly_engine import evaluate_threshold
from engines.slack_notifier import SlackUploader
from scheduler.runner import run_link_capture_and_alert
from scheduler.scheduler import execute_site_cycle, execute_cycle

# Load fallback HTML template from web.ui_template if available
try:
    from web.ui_template import HTML_TEMPLATE
except ImportError:
    HTML_TEMPLATE = "<!DOCTYPE html><html><body><h1>GreyOrange OpsBot UI</h1></body></html>"

TEMPLATE_FILE = os.path.join(os.path.dirname(__file__), "templates", "index.html")

app = Flask(
    __name__,
    static_folder=os.path.join(os.path.dirname(__file__), "static"),
    static_url_path="/static"
)


@app.route("/")
def index():
    """Serves the Web Operations Center Single Page Application."""
    if os.path.exists(TEMPLATE_FILE):
        try:
            with open(TEMPLATE_FILE, "r", encoding="utf-8") as f:
                return f.read(), 200, {"Content-Type": "text/html; charset=utf-8"}
        except Exception:
            pass
    return HTML_TEMPLATE, 200, {"Content-Type": "text/html; charset=utf-8"}


# ---------------------------------------------------------------------------
# Sites Management Endpoints
# ---------------------------------------------------------------------------

@app.route("/api/sites", methods=["GET"])
def get_sites():
    try:
        sites = SiteManager.get_all_sites()
        return jsonify({"success": True, "sites": sites})
    except Exception as e:
        return jsonify({"success": True, "sites": [], "error": str(e)}), 200


@app.route("/api/sites", methods=["POST"])
def create_site():
    try:
        data = request.get_json(silent=True) or {}
        name = str(data.get("name", "")).strip()
        if not name:
            return jsonify({"success": False, "error": "Site name cannot be empty."}), 400

        clone_from = data.get("clone_from_id") or None
        url = str(data.get("grafana_url", "")).strip()
        new_site = SiteManager.create_site(name=name, clone_from_id=clone_from, grafana_url=url)
        if not new_site:
            return jsonify({"success": False, "error": "Site creation failed internally."}), 500
        return jsonify({"success": True, "site": new_site})
    except Exception as e:
        bot_log(f"[ERROR] create_site exception: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/sites/<site_id>", methods=["GET"])
def get_single_site(site_id):
    site = SiteManager.get_site(site_id)
    if not site:
        return jsonify({"success": False, "error": "Site not found"}), 404
    return jsonify({"success": True, "site": site})


@app.route("/api/sites/<site_id>", methods=["POST"])
def update_single_site(site_id):
    try:
        data = request.get_json(silent=True) or {}
        ok, res = SiteManager.update_site(site_id, data)
        if not ok:
            return jsonify({"success": False, "error": res}), 400
        return jsonify({"success": True, "site": res})
    except Exception as e:
        bot_log(f"[ERROR] update_site exception: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/sites/<site_id>", methods=["DELETE"])
def delete_single_site(site_id):
    try:
        ok, msg = SiteManager.delete_site(site_id)
        if not ok:
            return jsonify({"success": False, "error": msg}), 400
        return jsonify({"success": True, "message": msg})
    except Exception as e:
        bot_log(f"[ERROR] delete_site exception: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/sites/<site_id>/toggle-pause", methods=["POST"])
def toggle_site_pause(site_id):
    ok, is_p = SiteManager.toggle_pause_site(site_id)
    if not ok:
        return jsonify({"success": False, "error": is_p}), 400
    return jsonify({"success": True, "is_paused": is_p})


# ---------------------------------------------------------------------------
# Link Management Endpoints (Chrome Tab-Strip)
# ---------------------------------------------------------------------------

@app.route("/api/sites/<site_id>/links", methods=["GET"])
def get_site_links_api(site_id):
    links = SiteManager.get_site_links(site_id)
    return jsonify({"success": True, "links": links})


@app.route("/api/sites/<site_id>/links", methods=["POST"])
def add_site_link_api(site_id):
    try:
        data = request.get_json(silent=True) or {}
        ok, res = SiteManager.add_site_link(site_id, data)
        if not ok:
            return jsonify({"success": False, "error": res}), 400
        return jsonify({"success": True, "link": res})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/sites/<site_id>/links/<link_id>", methods=["POST"])
def update_site_link_api(site_id, link_id):
    try:
        data = request.get_json(silent=True) or {}
        ok, res = SiteManager.update_site_link(site_id, link_id, data)
        if not ok:
            return jsonify({"success": False, "error": res}), 400
        return jsonify({"success": True, "link": res})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/sites/<site_id>/links/<link_id>", methods=["DELETE"])
def delete_site_link_api(site_id, link_id):
    try:
        ok, msg = SiteManager.delete_site_link(site_id, link_id)
        if not ok:
            return jsonify({"success": False, "error": msg}), 400
        return jsonify({"success": True, "message": msg})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/sites/<site_id>/links/<link_id>/toggle", methods=["POST"])
def toggle_site_link_api(site_id, link_id):
    ok, is_en = SiteManager.toggle_site_link(site_id, link_id)
    if not ok:
        return jsonify({"success": False, "error": is_en}), 400
    return jsonify({"success": True, "enabled": is_en})


@app.route("/api/sites/<site_id>/links/<link_id>/trigger", methods=["POST"])
def trigger_site_link_api(site_id, link_id):
    site = SiteManager.get_raw_site(site_id)
    if not site:
        return jsonify({"success": False, "error": "Site not found"}), 404

    links = SiteManager.get_site_links(site_id)
    target_link = next((lk for lk in links if lk["id"] == link_id), None)
    if not target_link:
        return jsonify({"success": False, "error": "Link not found"}), 404

    run_key = f"{site_id}_{link_id}"
    with ACTIVE_LINK_LOCK:
        if run_key in ACTIVE_LINK_RUNS:
            return jsonify({"success": False, "error": "A capture is already actively running for this link tab. Please wait."}), 409
        ACTIVE_LINK_RUNS.add(run_key)

    threading.Thread(target=run_link_capture_and_alert, args=(site_id, link_id, True, True), daemon=True).start()
    return jsonify({"success": True, "message": f"Capture triggered for link '{target_link.get('title')}'."})


@app.route("/api/sites/<site_id>/trigger", methods=["POST"])
def trigger_site_run(site_id):
    site = SiteManager.get_raw_site(site_id)
    if not site:
        return jsonify({"success": False, "error": "Site not found"}), 404

    s_st = SiteManager.get_site_state(site_id)
    if s_st.get("is_running"):
        return jsonify({"success": False, "error": "A capture cycle is already running for this site."}), 409

    t = threading.Thread(target=execute_site_cycle, args=(site_id, True), daemon=True)
    t.start()
    return jsonify({"success": True, "message": f"Capture cycle triggered across active links for {site.get('name', site_id)}."})


# ---------------------------------------------------------------------------
# Live Previews & Text Extraction Endpoints
# ---------------------------------------------------------------------------

@app.route("/api/preview-capture", methods=["POST"])
@app.route("/preview-capture", methods=["POST"])
@app.route("/api/sites/<site_id>/preview-capture", methods=["POST"])
@app.route("/api/sites/<site_id>/links/<link_id>/preview-capture", methods=["POST"])
def preview_site_capture(site_id=None, link_id=None):
    req_json = request.get_json(silent=True) or {}
    effective_site_id = site_id or req_json.get("site_id") or request.args.get("site_id")

    if not effective_site_id:
        sites = SiteManager.get_all_sites()
        if sites:
            effective_site_id = sites[0]["id"]
        else:
            return jsonify({"success": False, "error": "No sites configured."}), 404

    site = SiteManager.get_raw_site(effective_site_id)
    if not site:
        return jsonify({"success": False, "error": "Site not found"}), 404

    req_link_id = link_id or req_json.get("link_id") or request.args.get("link_id")
    links = SiteManager.get_site_links(effective_site_id)

    target_link = None
    if req_link_id:
        target_link = next((lk for lk in links if lk["id"] == req_link_id), None)
    if not target_link and links:
        target_link = links[0]

    url = target_link.get("url") if target_link else site.get("grafana_url")
    if target_link:
        time_range = target_link.get("time_range")
        if time_range and time_range != "url_default":
            url = apply_grafana_time_range(url, time_range)
        user, pwd, tok = resolve_link_auth(site, target_link, target_url=url)
        target_title = target_link.get("title", "Link")
    else:
        user, pwd, tok = resolve_link_auth(site, None, target_url=url)
        target_title = site.get("name", "Site")

    if not url:
        return jsonify({"success": False, "error": "No Grafana URL or monitored links configured."}), 400

    cleanup_old_previews()
    bot_log(f"[INFO] Headless snapshot preview request for '{site.get('name')} -> {target_title}'...", site_id=effective_site_id)

    try:
        capture = GrafanaCapture(site_dict=site)
        preview_filename = f"preview_{effective_site_id}_{int(time.time() * 1000)}.png"
        preview_path = os.path.join(tempfile.gettempdir(), preview_filename)

        is_bundle = bool(site.get("bundle_screenshots"))
        effective_hide_sidebar = bool(
            (target_link.get("hide_sidebar") if target_link else False) or
            (is_bundle and site.get("hide_sidebar")) or
            site.get("hide_sidebar", False)
        )

        with GLOBAL_CAPTURE_SEMAPHORE:
            capture.capture_screenshot(
                target_url=url,
                output_path=preview_path,
                username=user,
                password=pwd,
                token=tok,
                expand_row=(target_link.get("expand_row") or "Station Performance") if (target_link and target_link.get("expand_enabled")) else ("off" if target_link and target_link.get("expand_enabled") is False else None),
                hide_sidebar=effective_hide_sidebar
            )
        size_kb = os.path.getsize(preview_path) / 1024
        return jsonify({
            "success": True,
            "image_url": f"/api/preview-image/{preview_filename}",
            "message": f"Successfully captured snapshot for '{target_title}' ({size_kb:.1f} KB)"
        })
    except Exception as e:
        bot_log(f"[ERROR] [{site.get('name')} -> {target_title}] Preview capture failed: {e}", site_id=effective_site_id)
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/preview-extracted-text", methods=["POST"])
@app.route("/preview-extracted-text", methods=["POST"])
@app.route("/api/sites/<site_id>/preview-extracted-text", methods=["POST"])
@app.route("/api/sites/<site_id>/links/<link_id>/preview-extracted-text", methods=["POST"])
def preview_site_extracted_text(site_id=None, link_id=None):
    req_json = request.get_json(silent=True) or {}
    effective_site_id = site_id or req_json.get("site_id") or request.args.get("site_id")

    if not effective_site_id:
        sites = SiteManager.get_all_sites()
        if sites:
            effective_site_id = sites[0]["id"]
        else:
            return jsonify({"success": False, "error": "No sites configured."}), 404

    site = SiteManager.get_raw_site(effective_site_id)
    if not site:
        return jsonify({"success": False, "error": "Site not found"}), 404

    req_link_id = link_id or req_json.get("link_id") or request.args.get("link_id")
    links = SiteManager.get_site_links(effective_site_id)

    target_link = None
    if req_link_id:
        target_link = next((lk for lk in links if lk["id"] == req_link_id), None)
    if not target_link and links:
        target_link = links[0]

    url = target_link.get("url") if target_link else site.get("grafana_url")
    if target_link:
        time_range = target_link.get("time_range")
        if time_range and time_range != "url_default":
            url = apply_grafana_time_range(url, time_range)
        user, pwd, tok = resolve_link_auth(site, target_link, target_url=url)
        target_title = target_link.get("title", "Link")
        th_cfg = dict(target_link.get("threshold") or site.get("threshold", {}))
        shifts_cfg = target_link.get("shifts") or site.get("shifts", {})
    else:
        user, pwd, tok = resolve_link_auth(site, None, target_url=url)
        target_title = site.get("name", "Site")
        th_cfg = dict(site.get("threshold", {}))
        shifts_cfg = site.get("shifts", {})

    if "threshold_ai_mode" in req_json:
        th_cfg["ai_mode"] = bool(req_json["threshold_ai_mode"])
    if "threshold_ai_prompt" in req_json:
        th_cfg["ai_prompt"] = str(req_json["threshold_ai_prompt"]).strip()

    if not url:
        return jsonify({"success": False, "error": "No Grafana URL or monitored links configured."}), 400

    bot_log(f"[INFO] Dashboard data extraction request for '{site.get('name')} -> {target_title}'...", site_id=effective_site_id)
    temp_img = None
    try:
        capture = GrafanaCapture(site_dict=site)
        temp_img = os.path.join(tempfile.gettempdir(), f"preview_eval_{int(time.time() * 1000)}.png")
        is_bundle = bool(site.get("bundle_screenshots"))
        effective_hide_sidebar = bool(
            (target_link.get("hide_sidebar") if target_link else False) or
            (is_bundle and site.get("hide_sidebar")) or
            site.get("hide_sidebar", False)
        )

        with GLOBAL_CAPTURE_SEMAPHORE:
            output_path, extraction = capture.capture_screenshot(
                target_url=url,
                output_path=temp_img,
                username=user,
                password=pwd,
                token=tok,
                return_extracted=True,
                expand_row=(target_link.get("expand_row") or "Station Performance") if (target_link and target_link.get("expand_enabled")) else ("off" if target_link and target_link.get("expand_enabled") is False else None),
                hide_sidebar=effective_hide_sidebar
            )
        g_settings = SiteManager.get_global_settings()
        eval_res = evaluate_threshold(
            extraction,
            threshold_cfg=th_cfg,
            image_path=output_path,
            global_settings=g_settings,
            site_id=effective_site_id
        )
        active_shift = get_active_shift(shifts_cfg, SiteManager.get_timezone())

        return jsonify({
            "success": True,
            "link_title": target_title,
            "extraction": extraction,
            "evaluation": eval_res,
            "active_shift": active_shift
        })
    except Exception as e:
        bot_log(f"[ERROR] [{site.get('name')} -> {target_title}] Extraction failed: {e}", site_id=effective_site_id)
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if temp_img and os.path.exists(temp_img):
            try:
                os.remove(temp_img)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Slack Testing Endpoints
# ---------------------------------------------------------------------------

@app.route("/api/test-slack", methods=["POST"])
@app.route("/test-slack", methods=["POST"])
@app.route("/api/sites/<site_id>/test-slack", methods=["POST"])
@app.route("/api/sites/<site_id>/links/<link_id>/test-slack", methods=["POST"])
def test_site_slack(site_id=None, link_id=None):
    req_json = request.get_json(silent=True) or {}
    effective_site_id = site_id or req_json.get("site_id") or request.args.get("site_id")

    site = SiteManager.get_raw_site(effective_site_id) if effective_site_id else None
    if not site:
        sites = SiteManager.get_all_sites()
        site = sites[0] if sites else {}

    req_link_id = link_id or req_json.get("link_id") or request.args.get("link_id")
    target_link = None
    if effective_site_id and req_link_id:
        links = SiteManager.get_site_links(effective_site_id)
        target_link = next((lk for lk in links if lk["id"] == req_link_id), None)

    g_settings = SiteManager.get_global_settings()
    token = g_settings.get("slack_bot_token")
    chan = (target_link.get("slack_channel_id") if target_link else None) or site.get("slack_channel_id")
    thread_ts = (target_link.get("slack_thread_ts") if target_link else None) or site.get("slack_thread_ts")
    link_name = target_link.get("title") if target_link else site.get("name", "Site")

    uploader = SlackUploader(token=token, channel_id=chan, thread_ts=thread_ts)
    ok, details = uploader.test_auth()
    if not ok:
        return jsonify({"success": False, "error": str(details)}), 400

    user = details.get("user", "Bot")
    team = details.get("team", "Workspace")

    if not chan:
        return jsonify({
            "success": True,
            "message": f"Connected to '{team}' as '{user}'! (Slack Channel ID is empty for '{link_name}', so ping was not posted to a channel)."
        })

    post_ok, post_res = uploader.post_text_message(
        f"*Slack Test Ping Successful for {site.get('name', 'Site')} — {link_name}*\nConnected as `{user}` on `{team}` at {get_current_time_str(tz_name=SiteManager.get_timezone())}."
    )
    if post_ok:
        return jsonify({
            "success": True,
            "message": f"Connected to '{team}' as '{user}'! Test ping posted to channel {chan} for '{link_name}'."
        })
    return jsonify({
        "success": False,
        "error": f"Connected as '{user}' on '{team}', but posting to channel {chan} failed: {post_res}"
    }), 400


@app.route("/api/test-dm", methods=["POST"])
@app.route("/test-dm", methods=["POST"])
@app.route("/api/sites/<site_id>/test-dm", methods=["POST"])
@app.route("/api/sites/<site_id>/links/<link_id>/test-dm", methods=["POST"])
def test_site_dm(site_id=None, link_id=None):
    data = request.get_json(silent=True) or {}
    effective_site_id = site_id or data.get("site_id") or request.args.get("site_id")

    site = SiteManager.get_raw_site(effective_site_id) if effective_site_id else None
    if not site:
        sites = SiteManager.get_all_sites()
        site = sites[0] if sites else {}

    req_link_id = link_id or data.get("link_id") or request.args.get("link_id")
    target_link = None
    if effective_site_id and req_link_id:
        links = SiteManager.get_site_links(effective_site_id)
        target_link = next((lk for lk in links if lk["id"] == req_link_id), None)

    target_user = str(data.get("user_id") or "").strip()
    if target_user:
        uids = [u.strip().strip("<>@#") for u in target_user.replace(",", " ").split() if u.strip()]
        target_user = uids[0] if uids else ""

    if not target_user:
        threshold_cfg = (target_link.get("threshold") if target_link else None) or site.get("threshold", {})
        shifts_cfg = (target_link.get("shifts") if target_link else None) or site.get("shifts", {})
        tz = SiteManager.get_timezone()
        active_shift = get_active_shift(shifts_cfg, tz)

        raw_candidates = threshold_cfg.get("breach_users", "") + " " + active_shift.get("users_raw", "")
        extracted_uids = [u.strip().strip("<>@#") for u in raw_candidates.replace(",", " ").split() if u.strip()]
        if extracted_uids:
            target_user = extracted_uids[0]

    if not target_user:
        return jsonify({"success": False, "error": "Please provide a Slack Member ID (e.g. U01234567) to test DM."}), 400

    g_settings = SiteManager.get_global_settings()
    token = g_settings.get("slack_bot_token")
    uploader = SlackUploader(token=token)

    ok, details = uploader.test_auth()
    if not ok:
        return jsonify({"success": False, "error": f"Slack Authentication Failed: {details}"}), 400

    bot_user = details.get("user", "OpsBot")
    team = details.get("team", "Slack Workspace")
    site_name = site.get("name", "Site")
    link_title = target_link.get("title") if target_link else site_name
    now_str = get_current_time_str(tz_name=SiteManager.get_timezone())

    test_message = (
        f"*[OpsBot Direct Message Alert Test]*\n"
        f"Hello! This is an in-person test DM sent by *{bot_user}* on *{team}*.\n"
        f"• *Target Link:* {site_name} — {link_title}\n"
        f"• *Timestamp:* {now_str}\n"
        f"• *Delivery:* Direct Message (1-on-1)\n"
        f"Your in-person Slack DM alerting is active and working properly!"
    )

    dm_ok, dm_res = uploader.send_dm_text(target_user, test_message)
    if dm_ok:
        bot_log(f"[{site_name} -> {link_title}] Test DM delivered successfully to <@{target_user}>!", site_id=site.get("id"))
        return jsonify({
            "success": True,
            "message": f"Test DM delivered to <@{target_user}> for '{link_title}' successfully!"
        })
    bot_log(f"[ERROR] [{site_name} -> {link_title}] Test DM to <@{target_user}> failed: {dm_res}", site_id=site.get("id"))
    return jsonify({"success": False, "error": f"Failed sending DM to {target_user}: {dm_res}"}), 400


# ---------------------------------------------------------------------------
# Global Settings & Logs Endpoints
# ---------------------------------------------------------------------------

@app.route("/api/global-settings", methods=["GET", "POST"])
def handle_global_settings():
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        updated = SiteManager.update_global_settings(data)
        bot_log("[SUCCESS] Updated global hub settings (Slack Bot Token, Timezone, Proxy)")
        return jsonify({"success": True, "settings": updated})

    g = SiteManager.get_global_settings()
    actual_tok = g.get("slack_bot_token", "")
    g["slack_bot_token_set"] = bool(actual_tok)
    g["slack_bot_token"] = actual_tok
    actual_gem = g.get("gemini_api_key", "")
    g["gemini_api_key_set"] = bool(actual_gem)
    g["gemini_api_key"] = actual_gem
    return jsonify({"success": True, "settings": g})


@app.route("/api/ai/status", methods=["GET"])
def get_ai_engine_status():
    from ai_engine import get_ai_service
    svc = get_ai_service()
    return jsonify({"success": True, "ai_engine": svc.get_service_status()})


@app.route("/api/logs", methods=["GET"])
def get_logs():
    filter_site = request.args.get("site_id")
    if filter_site and filter_site != "all":
        logs = [l["formatted"] for l in LOG_BUFFER if l.get("site_id") == filter_site or l.get("site_id") == "global"]
    else:
        logs = [l["formatted"] for l in LOG_BUFFER]
    return jsonify({"logs": logs})


# ---------------------------------------------------------------------------
# Backward Compatibility & Legacy Endpoints
# ---------------------------------------------------------------------------

@app.route("/api/status", methods=["GET"])
def get_legacy_status():
    sites = SiteManager.get_all_sites()
    primary = sites[0] if sites else {}
    return jsonify({
        "status": "ok",
        "bot_state": primary.get("state", {}),
        "config": {
            "grafana_url": primary.get("grafana_url", ""),
            "slack_channel_id": primary.get("slack_channel_id", ""),
            "slack_thread_ts": primary.get("slack_thread_ts", ""),
            "interval": primary.get("interval_minutes", 30)
        }
    })


@app.route("/api/config", methods=["GET", "POST"])
def handle_legacy_config():
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        SiteManager.update_global_settings(data)
        sites = SiteManager.get_all_sites()
        if sites:
            SiteManager.update_site(sites[0]["id"], data)
        return jsonify({"success": True, "message": "Configuration updated successfully."})

    g = SiteManager.get_global_settings()
    sites = SiteManager.get_all_sites()
    primary = sites[0] if sites else {}
    return jsonify({"success": True, "global_settings": g, "site": primary, "config": primary})


@app.route("/api/auth/session-status", methods=["GET"])
def get_session_status():
    auth_path = os.path.join(BASE_DIR, "grafana_auth_state.json")
    if not os.path.isfile(auth_path):
        return jsonify({
            "exists": False,
            "message": "No master auth state file found on server.",
            "domains": [],
            "cookie_count": 0
        })
    try:
        with open(auth_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        cookies = data.get("cookies", [])
        domains = set()
        has_google = False
        for c in cookies:
            dom = c.get("domain", "").lstrip(".")
            if dom:
                domains.add(dom)
            if "google" in dom:
                has_google = True
        mtime = os.path.getmtime(auth_path)
        last_modified = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
        return jsonify({
            "exists": True,
            "has_google_sso": has_google,
            "cookie_count": len(cookies),
            "domains": sorted(list(domains)),
            "last_modified": last_modified,
            "message": "Master Google SSO session active." if has_google else "Auth state active."
        })
    except Exception as e:
        return jsonify({"exists": False, "error": str(e)}), 500


@app.route("/api/auth/upload-session", methods=["POST"])
def upload_session():
    auth_path = os.path.join(BASE_DIR, "grafana_auth_state.json")
    raw_content = None
    if "file" in request.files:
        f = request.files["file"]
        raw_content = f.read().decode("utf-8", errors="replace")
    else:
        req_json = request.get_json(silent=True) or {}
        raw_content = req_json.get("content") or req_json.get("session_json")

    if not raw_content:
        return jsonify({"success": False, "error": "No session JSON content provided."}), 400

    try:
        parsed = json.loads(raw_content)
        if not isinstance(parsed, dict) or "cookies" not in parsed:
            return jsonify({"success": False, "error": "Invalid format. Expected Playwright storage_state JSON with 'cookies'."}), 400

        with open(auth_path, "w", encoding="utf-8") as af:
            json.dump(parsed, af, indent=2)

        # Immediately sync tokens into sites.json
        SiteManager.sync_auth_state_to_sites()

        cookies = parsed.get("cookies", [])
        domains = sorted(list(set(c.get("domain", "").lstrip(".") for c in cookies if c.get("domain"))))
        bot_log(f"[AUTH] Master session state updated via Web UI ({len(cookies)} cookies across {len(domains)} domains).")
        return jsonify({
            "success": True,
            "message": f"Saved master session state successfully with {len(cookies)} cookies.",
            "domains": domains
        })
    except Exception as e:
        return jsonify({"success": False, "error": f"Failed to parse and save session: {e}"}), 400


@app.route("/api/trigger", methods=["POST"])
def trigger_all():
    execute_cycle()
    return jsonify({"success": True, "message": "Triggered all sites capture."})


@app.route("/api/toggle-pause", methods=["POST"])
def toggle_primary_pause():
    sites = SiteManager.get_all_sites()
    if not sites:
        return jsonify({"success": False, "error": "No sites"}), 400
    ok, is_p = SiteManager.toggle_pause_site(sites[0]["id"])
    return jsonify({"success": True, "is_paused": is_p})


@app.route("/api/preview-image/<filename>", methods=["GET"])
@app.route("/preview-image/<filename>", methods=["GET"])
def get_preview_image(filename):
    safe_name = os.path.basename(filename)
    path = os.path.join(tempfile.gettempdir(), safe_name)
    if os.path.exists(path) and os.path.isfile(path):
        return send_file(path, mimetype="image/png")
    return "Not found", 404


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


def ensure_app_logo():
    base_dir = BASE_DIR
    target_png = os.path.join(base_dir, "app_icon.png")
    if os.path.exists(target_png) and os.path.getsize(target_png) > 1000:
        return target_png

    try:
        from PIL import Image, ImageDraw
        img = Image.new("RGBA", (512, 512), color=(24, 24, 27, 255))
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle([(32, 32), (480, 480)], radius=96, fill=(255, 90, 0, 255))
        draw.ellipse([(140, 115), (372, 347)], outline=(255, 255, 255, 255), width=42)
        draw.rounded_rectangle([(330, 115), (372, 420)], radius=21, fill=(255, 255, 255, 255))
        draw.rounded_rectangle([(140, 378), (372, 420)], radius=21, fill=(255, 255, 255, 255))
        draw.line([(100, 231), (180, 231), (210, 175), (245, 287), (275, 231), (412, 231)], fill=(255, 255, 255, 230), width=12)
        img.save(target_png, "PNG")
        return target_png
    except Exception:
        return None


@app.route("/api/app-icon", methods=["GET"])
@app.route("/logo.png", methods=["GET"])
@app.route("/favicon.ico", methods=["GET"])
def get_app_icon():
    png_path = ensure_app_logo()
    if png_path and os.path.exists(png_path):
        return send_file(png_path, mimetype="image/png")
    return "Not found", 404


def run_web_server():
    g = SiteManager.get_global_settings()
    host = g.get("web_host", "0.0.0.0")
    port = safe_int(g.get("web_port"), 5000)
    bot_log(f"[INFO] Multi-Site Web Operations Center running at: http://{host}:{port}")
    app.run(host=host, port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    SiteManager.init()
    ensure_app_logo()
    run_web_server()
