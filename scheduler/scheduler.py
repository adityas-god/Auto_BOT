"""
Zero-Conflict Multi-Site Concurrency Scheduler for GreyOrange OpsBot.
Provides:
- Independent background execution loop across active sites
- Thread-level isolation preventing cross-site blocking
- Cadence evaluation per site and per monitored link
- Automatic execution cycle triggers
"""

import time
import threading

from core.config import ACTIVE_LINK_LOCK, ACTIVE_LINK_RUNS
from core.database import SiteManager
from core.logger import bot_log
from core.utils import safe_int, safe_float, get_current_time_str
from scheduler.runner import run_link_capture_and_alert, run_site_bundle_capture_and_alert


def execute_site_cycle(site_id, force_all=False):
    """Executes capture, threshold analysis, and upload for a specific site with zero conflicts."""
    site = SiteManager.get_raw_site(site_id)
    if not site:
        return

    site_name = site.get("name", site_id)
    site_lock = SiteManager.get_site_lock(site_id)

    # Prevent concurrent execution of the SAME site
    if not site_lock.acquire(blocking=False):
        bot_log(f"Cycle already running for '{site_name}', skipping trigger.", site_id=site_id)
        return

    # If scheduled background cycle (not manual force run), strictly enforce paused state
    if not force_all:
        raw_s = SiteManager.get_raw_site(site_id)
        s_st = SiteManager.get_site_state(site_id)
        if not raw_s or raw_s.get("paused", True) or s_st.get("is_paused", True) or not raw_s.get("enabled", True):
            site_lock.release()
            return

    try:
        SiteManager.set_site_state(site_id, is_running=True, last_status="Capturing...")
        links = SiteManager.get_site_links(site_id, raw=True)
        active_links = [lk for lk in links if (lk.get("enabled", True) or force_all) and lk.get("url")]

        if not active_links:
            bot_log(f"[{site_name}] No active or enabled monitored links configured for this site. Monitoring standing by.", site_id=site_id)
            SiteManager.set_site_state(site_id, last_status="No Active Links", is_running=False)
            return

        bundle_enabled = bool(site.get("bundle_screenshots", False))
        if bundle_enabled:
            ok, msg = run_site_bundle_capture_and_alert(site_id, force=force_all)
            return

        now_ts = time.time()
        tz = SiteManager.get_timezone()
        overall_errors = []

        for lk in active_links:
            lk_id = lk.get("id")
            run_key = f"{site_id}_{lk_id}"
            with ACTIVE_LINK_LOCK:
                if run_key in ACTIVE_LINK_RUNS:
                    continue

            if not force_all:
                lk_interval = safe_int(lk.get("interval_minutes"), 0)
                if lk_interval <= 0:
                    lk_interval = max(1, safe_int(site.get("interval_minutes"), 30))
                lk_last_ts = lk.get("last_run_ts") or 0
                if lk_last_ts > 0 and (now_ts - lk_last_ts) < (lk_interval * 60) - 5:
                    continue  # not due yet

            ok, msg = run_link_capture_and_alert(site_id, lk_id, force=force_all)
            if not ok and msg not in ("Cadence interval not due yet", "Link is disabled", "Already running"):
                overall_errors.append(f"{lk.get('title')}: {msg}")

        now_str = get_current_time_str(tz_name=tz)
        overall_status = f"Completed ({len(active_links)} links)" if not overall_errors else f"Warnings ({len(overall_errors)} links)"
        SiteManager.record_site_run(
            site_id=site_id,
            last_run_time=now_str,
            last_run_ts=time.time(),
            last_status=overall_status
        )

    except Exception as e:
        bot_log(f"[{site_name}] Execution error: {e}", site_id=site_id)
        SiteManager.set_site_state(site_id, last_status=f"Error: {e}")
    finally:
        SiteManager.set_site_state(site_id, is_running=False)
        site_lock.release()


def execute_cycle():
    """Trigger cycles only across active, non-paused sites."""
    sites = SiteManager.get_all_sites()
    for s in sites:
        sid = s["id"]
        state = SiteManager.get_site_state(sid)
        if s.get("enabled", True) and not s.get("paused", True) and not state.get("is_paused", True):
            t = threading.Thread(target=execute_site_cycle, args=(sid, False), daemon=True)
            t.start()


def scheduler_loop():
    """Continuous daemon loop running in the background."""
    time.sleep(3)
    bot_log("[INFO] Multi-Site Zero-Conflict Concurrency Scheduler resumed.")
    while True:
        try:
            sites = SiteManager.get_all_sites()
            now_ts = time.time()
            for site in sites:
                s_id = site["id"]
                s_state = SiteManager.get_site_state(s_id)
                if s_state.get("is_paused", True) or site.get("paused", True) or not site.get("enabled", True):
                    continue

                # Concurrency guard: If any link capture on this site is currently in flight, wait
                with ACTIVE_LINK_LOCK:
                    if any(rk.startswith(f"{s_id}_") for rk in ACTIVE_LINK_RUNS):
                        continue

                links = SiteManager.get_site_links(s_id)
                active_links = [lk for lk in links if lk.get("enabled", True) and lk.get("url")]
                bundle_enabled = bool(site.get("bundle_screenshots", False))
                site_interval = max(1, safe_int(site.get("interval_minutes"), 30))
                active_intervals = [safe_int(lk.get("interval_minutes"), 0) for lk in active_links if safe_int(lk.get("interval_minutes"), 0) > 0]
                effective_interval = min(active_intervals) if (bundle_enabled and active_intervals) else site_interval

                if not active_links:
                    continue

                is_due = False
                if bundle_enabled:
                    site_last_ts = safe_float(site.get("last_run_ts"), 0.0)
                    if site_last_ts <= 0:
                        is_due = True
                    elif (now_ts - site_last_ts) >= (effective_interval * 60) - 5:
                        is_due = True
                else:
                    for lk in active_links:
                        lk_id = lk.get("id")
                        run_key = f"{s_id}_{lk_id}"
                        with ACTIVE_LINK_LOCK:
                            if run_key in ACTIVE_LINK_RUNS:
                                continue
                        lk_interval = safe_int(lk.get("interval_minutes"), 0)
                        if lk_interval <= 0:
                            lk_interval = site_interval
                        lk_last_ts = safe_float(lk.get("last_run_ts"), 0.0)
                        if lk_last_ts <= 0:
                            is_due = True
                            break
                        if (now_ts - lk_last_ts) >= (lk_interval * 60) - 5:
                            is_due = True
                            break

                if is_due and not s_state.get("is_running", False):
                    # Immediately record run timestamp to lock out repeated triggers in subsequent scheduler loop iterations
                    SiteManager.set_site_state(s_id, is_running=True)
                    SiteManager.record_site_run(
                        s_id,
                        last_run_time=get_current_time_str(tz_name=SiteManager.get_timezone()),
                        last_run_ts=now_ts,
                        last_status="Scheduled..."
                    )
                    # Spawn dedicated worker thread for this site (Zero-conflict parallel execution)
                    t = threading.Thread(target=execute_site_cycle, args=(s_id, False), daemon=True)
                    t.start()
        except Exception as e:
            bot_log(f"[ERROR] Scheduler master loop error: {e}")

        time.sleep(3)
