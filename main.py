"""
==============================================================================
GREYORANGE OPSBOT — MULTI-SITE HEADLESS MONITORING HUB
==============================================================================
Main Entry Point & Modular Orchestrator.

Architecture:
  - core/      : Base configuration, environment, database sync, logger & utilities.
  - engines/   : Headless Playwright capture, DOM extractor, OCR, anomaly engine & Slack uploader.
  - scheduler/ : Concurrent link execution, multi-site cycle orchestrator & scheduler daemon.
  - web/       : Flask Operations Center REST API server and modern web interface.
==============================================================================
"""

import os
import sys
import time
import argparse
import threading

# Re-exports for complete backward compatibility
from core.config import BASE_DIR, SITES_PATH, ACTIVE_LINK_LOCK, ACTIVE_LINK_RUNS, GLOBAL_CAPTURE_SEMAPHORE
from core.logger import bot_log, LOG_BUFFER
from core.utils import safe_int, get_current_datetime, get_current_time_str, apply_grafana_time_range
from core.shifts import get_active_shift
from core.auth import resolve_link_auth
from core.database import SiteManager, Config
from engines.ocr_engine import inspect_screenshot_pixels, run_ocr_on_screenshot, extract_numbers_from_text
from engines.page_extractor import extract_page_data
from engines.anomaly_engine import evaluate_threshold
from engines.slack_notifier import SlackUploader
from engines.grafana_capture import GrafanaCapture
from scheduler.runner import run_link_capture_and_alert
from scheduler.scheduler import execute_site_cycle, execute_cycle, scheduler_loop
from web.ui_template import HTML_TEMPLATE
from web.app import app, run_web_server, ensure_app_logo


def main():
    parser = argparse.ArgumentParser(description="GreyOrange Multi-Site Headless Monitoring Hub")
    parser.add_argument("--once", action="store_true", help="Capture & upload once for all active sites, then exit")
    parser.add_argument("--ui-only", action="store_true", help="Start only the Web Management UI without the background scheduler")
    parser.add_argument("--no-web", action="store_true", help="Start only the scheduler loop without starting the web server")
    parser.add_argument("--test-slack", action="store_true", help="Verify Slack bot credentials and post a test ping message")
    parser.add_argument("--test-grafana", action="store_true", help="Take a headless screenshot of active Grafana site link(s) and save locally")
    args = parser.parse_args()

    print("[STARTUP] GreyOrange OpsBot initialising...", flush=True)
    print("[STARTUP] Loading site configuration from MongoDB Atlas...", flush=True)
    SiteManager.init()
    print("[STARTUP] Site configuration loaded successfully.", flush=True)
    ensure_app_logo()

    # Automatic cleanup of temporary test images from workspace
    for tmp_f in ("test_capture_result.png", "test_auth.png", "Untitled-1.txt"):
        tmp_fp = os.path.join(BASE_DIR, tmp_f)
        if os.path.exists(tmp_fp):
            try:
                os.remove(tmp_fp)
            except Exception:
                pass

    if args.test_slack:
        bot_log("[INFO] Testing Slack credentials...")
        g = SiteManager.get_global_settings()
        sites = SiteManager.get_all_sites()
        primary_chan = sites[0].get("slack_channel_id") if sites else None
        uploader = SlackUploader(token=g.get("slack_bot_token"), channel_id=primary_chan)
        ok, details = uploader.test_auth()
        if not ok:
            bot_log(f"[ERROR] Slack authentication failed: {details}")
            sys.exit(1)
        user = details.get("user", "Bot")
        team = details.get("team", "Workspace")
        bot_log(f"[SUCCESS] Authenticated with Slack as '{user}' on workspace '{team}'!")
        if primary_chan:
            post_ok, post_res = uploader.post_text_message(
                f"*Slack Test Ping Successful*\nCLI test executed at {get_current_time_str(tz_name=SiteManager.get_timezone())}."
            )
            if post_ok:
                bot_log(f"[SUCCESS] Test message posted to channel {primary_chan} successfully!")
            else:
                bot_log(f"[ERROR] Failed to post message to channel {primary_chan}: {post_res}")
                sys.exit(1)
        return

    if args.test_grafana:
        bot_log("[INFO] Testing Grafana screenshot capture across sites...")
        sites = SiteManager.get_all_sites()
        if not sites:
            bot_log("[ERROR] No sites configured!")
            sys.exit(1)
        for idx, site in enumerate(sites, 1):
            u = site.get("grafana_url")
            if not u:
                continue
            local_out = f"test_capture_site_{idx}.png"
            bot_log(f"[INFO] Testing capture for site '{site.get('name')}': {u} -> {local_out}")
            try:
                capture = GrafanaCapture(site_dict=site)
                capture.capture_screenshot(target_url=u, output_path=local_out)
                bot_log(f"[SUCCESS] Successfully captured to {local_out} ({os.path.getsize(local_out)/1024:.1f} KB)")
            except Exception as e:
                bot_log(f"[ERROR] Capture failed for site '{site.get('name')}': {e}")
        return

    if args.once:
        execute_cycle()
        return

    if args.no_web:
        bot_log("=" * 65)
        bot_log("[INFO] GreyOrange Multi-Site Daemon Mode (No Web UI)")
        bot_log("=" * 65)
        try:
            scheduler_loop()
        except KeyboardInterrupt:
            bot_log("\n[INFO] Bot stopped.")
        return

    g = SiteManager.get_global_settings()
    host = g.get("web_host", "0.0.0.0")
    port = safe_int(g.get("web_port"), 5000)
    bot_log("=" * 65)
    bot_log("[INFO] GreyOrange OpsBot — Multi-Site Operations Center")
    bot_log(f"[INFO] Web Operations Center: http://{host}:{port}")
    bot_log(f"[INFO] Total Sites Configured: {len(SiteManager.get_all_sites())}")
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
