#!/usr/bin/env python3
"""
GreyOrange OpsBot - Multi-Site Headless Monitoring & Operations Hub
- Unified Multi-Site Monitoring Architecture with Zero-Conflict Concurrency
- Isolated Playwright Browser Contexts per site
- Global Concurrency Semaphore to keep VM Memory < 450 MB
- Left Sidebar Site Switcher Dock (Add / Switch / Monitor multiple sites)
- Scoped Activity Stream Console with per-site filtering
- 100% Headless browser capture (Playwright Chromium)
- Modern 3-step Slack S3 Upload API
- MongoDB Atlas persistence for site configuration
"""

import os
import sys
import socket
import re
import time
import json
import tempfile
import threading
import argparse
from collections import deque
from datetime import datetime
from urllib.parse import urlparse
import subprocess

try:
    from pymongo import MongoClient
    from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError
    _PYMONGO_AVAILABLE = True
except ImportError:
    _PYMONGO_AVAILABLE = False

try:
    import pytesseract
    from PIL import Image
except ImportError:
    pytesseract = None
    Image = None

try:
    import pytz
except ImportError:
    pytz = None

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

import requests
from dotenv import load_dotenv
from flask import Flask, render_template_string, request, jsonify, send_file
from playwright.sync_api import sync_playwright

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE_DIR, ".env")
SITES_PATH = os.path.join(BASE_DIR, "sites.json")

if os.path.isdir(ENV_PATH):
    ENV_PATH = os.path.join(ENV_PATH, "settings.env")

if os.path.exists(ENV_PATH) and os.path.isfile(ENV_PATH):
    load_dotenv(dotenv_path=ENV_PATH, override=True)

# MongoDB configuration – prefer env var, fall back to hard-coded URI
MONGODB_URI = os.getenv(
    "MONGODB_URI",
    "mongodb+srv://adityasctr_db_user:ydv9XNaewJtaQjYC@cluster0.ef4bjjj.mongodb.net/operations_db?retryWrites=true&w=majority"
)
MONGO_DB_NAME = "operations_db"
# Dedicated collections for this project — completely isolated, no shared data
MONGO_SITES_COLLECTION = "opsbot_sites"
MONGO_SETTINGS_COLLECTION = "opsbot_global_settings"


def _make_json_safe(obj):
    """Recursively convert non-JSON-serializable types (datetime, ObjectId, bytes, etc.) to strings."""
    if isinstance(obj, dict):
        return {k: _make_json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_make_json_safe(v) for v in obj]
    if isinstance(obj, datetime):
        return obj.isoformat()
    # Handle pymongo ObjectId and other BSON types
    try:
        json.dumps(obj)
        return obj
    except (TypeError, ValueError):
        return str(obj)


def safe_int(val, default):
    try:
        if val is None:
            return default
        s = str(val).strip()
        return int(s) if s else default
    except (ValueError, TypeError):
        return default


def get_current_datetime(tz_name="Asia/Kolkata"):
    target_tz = tz_name or "Asia/Kolkata"
    if pytz:
        try:
            return datetime.now(pytz.timezone(target_tz))
        except Exception:
            pass
    if ZoneInfo:
        try:
            return datetime.now(ZoneInfo(target_tz))
        except Exception:
            pass
    return datetime.now()


def get_current_time_str(fmt="%Y-%m-%d %H:%M:%S", tz_name="Asia/Kolkata"):
    return get_current_datetime(tz_name).strftime(fmt)


def is_proxy_available(proxy_str):
    if not proxy_str or not proxy_str.strip():
        return False
    try:
        clean = proxy_str.strip()
        parsed = urlparse(clean)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 1080
        with socket.create_connection((host, port), timeout=0.8):
            return True
    except Exception:
        return False


def get_effective_proxy(configured_proxy=None):
    """
    Seamless zero-config proxy resolver:
    1. If configured_proxy is explicitly set and reachable, use it.
    2. If on Linux/VM, auto-detects and uses local Cloudflare Zero-Trust warp-proxy (socks5://127.0.0.1:1080).
    3. On local Windows testing, if warp-proxy is not running, seamlessly bypasses proxy so tests pass directly.
    """
    candidates = []
    if configured_proxy and configured_proxy.strip():
        candidates.append(configured_proxy.strip())
    # Standard Cloudflare Zero Trust proxy deployed on GCP VM (cac-automation)
    candidates.append("socks5://127.0.0.1:1080")
    candidates.append("http://127.0.0.1:1080")

    for cand in candidates:
        if is_proxy_available(cand):
            return cand
    return None


def format_slack_message(template, target_url="", title="Grafana Snapshot", tz_name="Asia/Kolkata", trigger="", shift=""):
    now = get_current_datetime(tz_name)
    formatted_datetime = now.strftime("%Y-%m-%d %I:%M:%S %p")
    formatted_date = now.strftime("%Y-%m-%d")
    formatted_time = now.strftime("%I:%M:%S %p")

    msg = template or "*Grafana Snapshot Alert* - {datetime}"
    msg = msg.replace("\\n", "\n")
    msg = msg.replace("{datetime}", formatted_datetime)
    msg = msg.replace("{date}", formatted_date)
    msg = msg.replace("{time}", formatted_time)
    msg = msg.replace("{grafana_url}", target_url or "")
    msg = msg.replace("{title}", title or "Grafana Snapshot")
    msg = msg.replace("{site_name}", title or "Grafana Snapshot")
    msg = msg.replace("{site}", title or "Grafana Snapshot")
    msg = msg.replace("{trigger}", trigger or "")
    msg = msg.replace("{shift}", shift or "")
    return msg


# ==============================================================================
# MULTI-SITE ARCHITECTURE & SITE MANAGER (MongoDB-backed)
# ==============================================================================
# Limits concurrent browser launches across all sites to prevent VM memory spikes
GLOBAL_CAPTURE_SEMAPHORE = threading.Semaphore(2)

# ---------------------------------------------------------------------------
# MongoDB connection – module-level singleton (thread-safe)
# ---------------------------------------------------------------------------
_mongo_client = None
_mongo_db     = None
_mongo_conn_lock  = threading.Lock()   # Ensures only one thread creates the client
_mongo_write_lock = threading.Lock()   # Serialises background write threads


def _get_mongo_db():
    """Return a cached MongoDB database handle.  Thread-safe singleton."""
    global _mongo_client, _mongo_db
    if _mongo_db is not None:           # fast-path: already connected
        return _mongo_db
    with _mongo_conn_lock:              # only one thread enters the slow path
        if _mongo_db is not None:       # re-check after acquiring lock
            return _mongo_db
        if not _PYMONGO_AVAILABLE:
            raise RuntimeError("pymongo not installed. Run: py -m pip install 'pymongo>=4.6.0'")
        print("[MONGO] Connecting to MongoDB Atlas...", flush=True)
        client = MongoClient(
            MONGODB_URI,
            serverSelectionTimeoutMS=12000,
            connectTimeoutMS=12000,
            socketTimeoutMS=30000,
            retryWrites=True,
        )
        client.admin.command("ping")    # raises on failure
        _mongo_client = client
        _mongo_db     = client[MONGO_DB_NAME]
        print(f"[MONGO] Connected → {MONGO_DB_NAME}", flush=True)
        return _mongo_db


class SiteManager:
    _lock = threading.Lock()
    # In-memory cache: {"global_settings": {...}, "sites": [...]}
    _data = None
    _site_locks = {}
    _site_states = {}

    # ------------------------------------------------------------------
    # Default builders
    # ------------------------------------------------------------------
    @classmethod
    def _default_global_settings(cls):
        return {
            "slack_bot_token": os.getenv("SLACK_BOT_TOKEN", "").strip(),
            "timezone": os.getenv("TIMEZONE", "Asia/Kolkata").strip() or "Asia/Kolkata",
            "web_host": os.getenv("WEB_HOST", "0.0.0.0").strip() or "0.0.0.0",
            "web_port": safe_int(os.getenv("WEB_PORT"), 5000),
            "http_proxy": os.getenv("HTTP_PROXY", "").strip(),
            "viewport_width": safe_int(os.getenv("VIEWPORT_WIDTH"), 1920),
            "viewport_height": safe_int(os.getenv("VIEWPORT_HEIGHT"), 1080),
            "page_load_wait_seconds": safe_int(os.getenv("PAGE_LOAD_WAIT_SECONDS"), 8),
            "grafana_theme": os.getenv("GRAFANA_THEME", "dark").strip() or "dark"
        }

    @classmethod
    def _default_site_from_env(cls):
        return {
            "id": "site_1",
            "name": "Site 1 (Primary)",
            "enabled": True,
            "paused": os.getenv("BOT_PAUSED", "false").strip().lower() == "true",
            "interval_minutes": safe_int(os.getenv("SCHEDULE_INTERVAL_MINUTES"), 30),
            "grafana_url": os.getenv("GRAFANA_URL", "").strip(),
            "grafana_username": os.getenv("GRAFANA_USERNAME", "").strip(),
            "grafana_password": os.getenv("GRAFANA_PASSWORD", "").strip(),
            "grafana_token": os.getenv("GRAFANA_API_TOKEN", "").strip(),
            "slack_channel_id": os.getenv("SLACK_CHANNEL_ID", "").strip(),
            "slack_thread_ts": os.getenv("SLACK_THREAD_TS", "").strip(),
            "slack_message": (os.getenv("SLACK_MESSAGE") or os.getenv("SLACK_MESSAGE_TEMPLATE") or "*Grafana Snapshot Alert* - {datetime}").strip(),
            "threshold": {
                "enabled": os.getenv("THRESHOLD_ENABLED", "true").strip().lower() == "true",
                "metric_type": os.getenv("THRESHOLD_METRIC_TYPE", "row_count").strip() or "row_count",
                "operator": os.getenv("THRESHOLD_OPERATOR", ">").strip() or ">",
                "value": os.getenv("THRESHOLD_VALUE", "0").strip() or "0",
                "keywords": os.getenv("THRESHOLD_KEYWORDS", "").strip(),
                "colors": os.getenv("THRESHOLD_COLORS", "red, orange, yellow").strip(),
                "breach_users": os.getenv("THRESHOLD_BREACH_USERS", "").strip(),
                "only_alert_on_breach": os.getenv("THRESHOLD_ONLY_ALERT_ON_BREACH", "false").strip().lower() == "true"
            },
            "shifts": {
                "morning_hours": os.getenv("SHIFT_MORNING_HOURS", "06:00-14:00").strip() or "06:00-14:00",
                "morning_users": os.getenv("SHIFT_MORNING_USERS", "").strip(),
                "afternoon_hours": os.getenv("SHIFT_AFTERNOON_HOURS", "14:00-22:00").strip() or "14:00-22:00",
                "afternoon_users": os.getenv("SHIFT_AFTERNOON_USERS", "").strip(),
                "night_hours": os.getenv("SHIFT_NIGHT_HOURS", "22:00-06:00").strip() or "22:00-06:00",
                "night_users": os.getenv("SHIFT_NIGHT_USERS", "").strip(),
                "tag_channel": os.getenv("SHIFT_TAG_CHANNEL", "true").strip().lower() == "true",
                "send_dm": os.getenv("SHIFT_SEND_DM", "true").strip().lower() == "true"
            }
        }

    # ------------------------------------------------------------------
    # MongoDB helpers
    # ------------------------------------------------------------------
    @classmethod
    def _mongo_load(cls):
        """
        Load all data from MongoDB into the in-memory cache.
        Returns a dict: {"global_settings": {...}, "sites": [...]}
        All values are sanitised to be JSON-safe (no datetime / ObjectId).
        """
        db = _get_mongo_db()
        # Global settings – stored as a single document with _id="global"
        gs_doc = db[MONGO_SETTINGS_COLLECTION].find_one({"_id": "global"})
        if gs_doc:
            gs_doc.pop("_id", None)
            global_settings = _make_json_safe(dict(gs_doc))
        else:
            global_settings = cls._default_global_settings()

        # Sites – each site is a document with _id == site["id"]
        sites = []
        for doc in db[MONGO_SITES_COLLECTION].find({}).sort("_order", 1):
            mongo_id = doc.pop("_id", None)
            doc.pop("_order", None)
            doc = _make_json_safe(dict(doc))
            # Guarantee the 'id' field always exists (backward compat with old docs)
            if "id" not in doc:
                doc["id"] = str(mongo_id) if mongo_id else f"site_{int(time.time())}"
            sites.append(doc)

        return {"global_settings": global_settings, "sites": sites}

    @classmethod
    def _mongo_save_all_no_lock(cls):
        """
        Persist the full in-memory cache to MongoDB.
        Uses print() only – never bot_log() – to avoid re-entrant lock deadlock.
        Caller must hold cls._lock OR pass a snapshot of the data.
        """
        if not cls._data:
            return
        try:
            db = _get_mongo_db()

            # Upsert global settings
            gs = dict(cls._data.get("global_settings", {}))
            db[MONGO_SETTINGS_COLLECTION].replace_one(
                {"_id": "global"},
                {"_id": "global", **gs},
                upsert=True
            )

            # Upsert every site
            for order, site in enumerate(cls._data.get("sites", [])):
                site_doc = dict(site)
                site_id = site_doc["id"]
                db[MONGO_SITES_COLLECTION].replace_one(
                    {"_id": site_id},
                    {"_id": site_id, "_order": order, **site_doc},
                    upsert=True
                )

            # Remove documents for sites that no longer exist
            current_ids = [s["id"] for s in cls._data.get("sites", [])]
            db[MONGO_SITES_COLLECTION].delete_many({"_id": {"$nin": current_ids}})

        except Exception as exc:
            print(f"[MONGO][ERROR] Failed to persist to MongoDB: {exc}", flush=True)

    # ------------------------------------------------------------------
    # Initialisation & Local Fallback
    # ------------------------------------------------------------------
    _mongo_ready  = False   # True once background thread confirms Atlas connection
    _init_started = False   # Prevents spawning multiple background threads

    @classmethod
    def _load_local_sites_file(cls):
        """Synchronously load local sites.json if available."""
        if os.path.exists(SITES_PATH) and os.path.isfile(SITES_PATH):
            try:
                with open(SITES_PATH, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                    if content:
                        loaded = json.loads(content)
                        if isinstance(loaded, dict) and "sites" in loaded:
                            return _make_json_safe(loaded)
            except Exception as e:
                print(f"[STARTUP][WARN] Could not parse local sites.json: {e}", flush=True)
        return None

    @classmethod
    def init(cls):
        """Initialise in-memory cache synchronously from local backup and trigger background Atlas sync."""
        with cls._lock:
            if cls._data is not None:
                return

            local_data = cls._load_local_sites_file()
            if local_data and local_data.get("sites"):
                cls._data = local_data
                defaults = cls._default_global_settings()
                gs = cls._data.setdefault("global_settings", defaults)
                for k, v in defaults.items():
                    gs.setdefault(k, v)
                print(f"[STARTUP] Loaded {len(cls._data['sites'])} site(s) immediately from local storage.", flush=True)
            else:
                cls._data = {
                    "global_settings": cls._default_global_settings(),
                    "sites": []
                }
                default_site = cls._default_site_from_env()
                if default_site.get("grafana_url"):
                    cls._data["sites"].append(default_site)

            now_ts = time.time()
            for site in cls._data.get("sites", []):
                sid = site["id"]
                cls._site_locks.setdefault(sid, threading.Lock())
                # Preserve each site's own individual paused setting strictly
                is_p = bool(site.get("paused", False))
                interval_sec = max(60, safe_int(site.get("interval_minutes"), 30) * 60)
                last_run_time = site.get("last_run_time")
                last_run_ts = site.get("last_run_ts")

                if is_p:
                    next_due = 0
                    stat = "Paused"
                else:
                    if last_run_ts and (now_ts - last_run_ts < interval_sec):
                        next_due = last_run_ts + interval_sec
                    else:
                        next_due = now_ts + interval_sec
                    stat = "Idle"

                cls._site_states[sid] = {
                    "is_running": False,
                    "is_paused": is_p,
                    "last_run_time": last_run_time,
                    "last_run_ts": last_run_ts,
                    "last_status": stat,
                    "next_run_due": next_due,
                }

            should_start = not cls._init_started
            cls._init_started = True

        if should_start:
            t = threading.Thread(target=cls._mongo_connect_background, daemon=True, name="mongo-init")
            t.start()

    @classmethod
    def _mongo_connect_background(cls):
        """
        Background thread: Connect to Atlas, merge with in-memory sites.
        """
        try:
            _get_mongo_db()
            mongo_loaded = cls._mongo_load_safe()

            with cls._lock:
                now_ts = time.time()
                if mongo_loaded.get("sites"):
                    # MongoDB Atlas is the authoritative source of truth.
                    # Merge: Atlas sites completely replace/update site definitions.
                    atlas_ids = {s["id"] for s in mongo_loaded["sites"]}
                    # Preserve any brand-new locally added site that hasn't been written to Atlas yet
                    unpushed_local_sites = [s for s in cls._data.get("sites", []) if s["id"] not in atlas_ids]

                    merged_sites = []
                    for ms in mongo_loaded["sites"]:
                        ms_id = ms["id"]
                        # Preserve volatile runtime state if present in memory
                        local_target = next((s for s in cls._data.get("sites", []) if s["id"] == ms_id), None)
                        if local_target:
                            for rt_key in ("last_run_time", "last_run_ts", "last_status"):
                                if local_target.get(rt_key) and not ms.get(rt_key):
                                    ms[rt_key] = local_target[rt_key]
                        merged_sites.append(ms)

                    cls._data["sites"] = merged_sites + unpushed_local_sites

                    loaded_gs = mongo_loaded.get("global_settings", {})
                    local_gs = cls._data.setdefault("global_settings", {})
                    for k, v in loaded_gs.items():
                        if v is not None and v != "":
                            local_gs[k] = v

                defaults = cls._default_global_settings()
                gs = cls._data.setdefault("global_settings", defaults)
                for k, v in defaults.items():
                    gs.setdefault(k, v)

                for site in cls._data.get("sites", []):
                    sid = site["id"]
                    cls._site_locks.setdefault(sid, threading.Lock())
                    if sid not in cls._site_states:
                        is_p = bool(site.get("paused", False))
                        interval_sec = max(60, safe_int(site.get("interval_minutes"), 30) * 60)
                        last_ts = site.get("last_run_ts")
                        if is_p:
                            next_due = 0
                            stat = "Paused"
                        else:
                            if last_ts and (now_ts - last_ts < interval_sec):
                                next_due = last_ts + interval_sec
                            else:
                                next_due = now_ts + interval_sec
                            stat = "Idle"

                        cls._site_states[sid] = {
                            "is_running": False,
                            "is_paused": is_p,
                            "last_run_time": site.get("last_run_time"),
                            "last_run_ts": site.get("last_run_ts"),
                            "last_status": stat,
                            "next_run_due": next_due,
                        }

                snapshot = json.loads(json.dumps(cls._data))
                cls._mongo_ready = True

            # Push synchronized state to Atlas and local file
            cls._bg_push_snapshot(snapshot)
            try:
                with open(SITES_PATH, "w", encoding="utf-8") as f:
                    json.dump(snapshot, f, indent=2)
            except Exception:
                pass
            n = len(snapshot.get("sites", []))
            print(f"[MONGO] Ready. {n} site(s) synced with Atlas.", flush=True)

        except Exception as exc:
            print(f"[MONGO] Background init notice: {exc}. Bot continues with local storage.", flush=True)

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------
    @classmethod
    def _mongo_load_safe(cls):
        """
        Load all data from MongoDB.  No lock required — caller decides.
        Returns {"global_settings": {...}, "sites": [...]}, fully JSON-safe.
        """
        db = _get_mongo_db()
        gs_doc = db[MONGO_SETTINGS_COLLECTION].find_one({"_id": "global"})
        if gs_doc:
            gs_doc.pop("_id", None)
            global_settings = _make_json_safe(dict(gs_doc))
        else:
            global_settings = cls._default_global_settings()

        sites = []
        for doc in db[MONGO_SITES_COLLECTION].find({}).sort("_order", 1):
            mongo_id = str(doc.pop("_id", ""))
            doc.pop("_order", None)
            doc = _make_json_safe(dict(doc))
            if "id" not in doc:
                doc["id"] = mongo_id or f"site_{int(time.time())}"
            sites.append(doc)

        return {"global_settings": global_settings, "sites": sites}

    # keep old name as alias for code that calls it
    @classmethod
    def _mongo_load(cls):
        return cls._mongo_load_safe()

    @classmethod
    def _bg_push_snapshot(cls, snapshot):
        """Push a JSON-safe snapshot dict to Atlas in a daemon thread."""
        def _worker(snap):
            with _mongo_write_lock:
                try:
                    db = _get_mongo_db()
                    gs = dict(snap.get("global_settings", {}))
                    db[MONGO_SETTINGS_COLLECTION].replace_one(
                        {"_id": "global"}, {"_id": "global", **gs}, upsert=True
                    )
                    for order, site in enumerate(snap.get("sites", [])):
                        s = dict(site)
                        sid = s["id"]
                        db[MONGO_SITES_COLLECTION].replace_one(
                            {"_id": sid}, {"_id": sid, "_order": order, **s}, upsert=True
                        )
                    keep_ids = [s["id"] for s in snap.get("sites", [])]
                    db[MONGO_SITES_COLLECTION].delete_many({"_id": {"$nin": keep_ids}})
                    print(f"[MONGO] Saved {len(snap.get('sites',[]))} site(s) to Atlas.", flush=True)
                except Exception as exc:
                    print(f"[MONGO][ERROR] Save failed: {exc}", flush=True)
        threading.Thread(target=_worker, args=(snapshot,), daemon=True, name="mongo-save").start()

    @classmethod
    def _save_data_no_lock(cls):
        """
        Called while cls._lock is held.
        1. Writes sites.json instantly (local backup, fast).
        2. Fires a background thread to push to Atlas — never blocks the caller.
        """
        # 1. Local backup
        try:
            with open(SITES_PATH, "w", encoding="utf-8") as f:
                json.dump(cls._data, f, indent=2)
        except Exception as exc:
            print(f"[WARN] sites.json write failed: {exc}", flush=True)

        # 2. Deep-copy snapshot (still inside lock, but just json round-trip — fast)
        snapshot = json.loads(json.dumps(cls._data))
        # Atlas push happens outside the lock via daemon thread
        cls._bg_push_snapshot(snapshot)

        # 3. Mirror to .env for backward compat
        cls._sync_env_file_no_lock()

    @classmethod
    def _sync_env_file_no_lock(cls):
        """Maintains backward compatibility by mirroring primary site and global settings to .env"""
        if not cls._data or not cls._data.get("sites"):
            return
        try:
            site1 = cls._data["sites"][0]
            g = cls._data.get("global_settings", {})
            env_map = {
                "GRAFANA_URL": site1.get("grafana_url", ""),
                "GRAFANA_USERNAME": site1.get("grafana_username", ""),
                "GRAFANA_PASSWORD": site1.get("grafana_password", ""),
                "GRAFANA_API_TOKEN": site1.get("grafana_token", ""),
                "SLACK_BOT_TOKEN": g.get("slack_bot_token", ""),
                "SLACK_CHANNEL_ID": site1.get("slack_channel_id", ""),
                "SLACK_THREAD_TS": site1.get("slack_thread_ts", ""),
                "SLACK_MESSAGE": site1.get("slack_message", ""),
                "SLACK_MESSAGE_TEMPLATE": site1.get("slack_message", ""),
                "SCHEDULE_INTERVAL_MINUTES": str(site1.get("interval_minutes", 30)),
                "BOT_PAUSED": "true" if site1.get("paused") else "false",
                "TIMEZONE": g.get("timezone", "Asia/Kolkata"),
                "WEB_HOST": g.get("web_host", "0.0.0.0"),
                "WEB_PORT": str(g.get("web_port", 5000)),
                "HTTP_PROXY": g.get("http_proxy", ""),
                "VIEWPORT_WIDTH": str(g.get("viewport_width", 1920)),
                "VIEWPORT_HEIGHT": str(g.get("viewport_height", 1080)),
                "PAGE_LOAD_WAIT_SECONDS": str(g.get("page_load_wait_seconds", 8)),
                "GRAFANA_THEME": g.get("grafana_theme", "dark"),
            }
            thresh = site1.get("threshold", {})
            env_map.update({
                "THRESHOLD_ENABLED": "true" if thresh.get("enabled") else "false",
                "THRESHOLD_METRIC_TYPE": thresh.get("metric_type", "row_count"),
                "THRESHOLD_OPERATOR": thresh.get("operator", ">"),
                "THRESHOLD_VALUE": thresh.get("value", "0"),
                "THRESHOLD_KEYWORDS": thresh.get("keywords", ""),
                "THRESHOLD_COLORS": thresh.get("colors", "red, orange, yellow"),
                "THRESHOLD_ONLY_ALERT_ON_BREACH": "true" if thresh.get("only_alert_on_breach") else "false",
                "THRESHOLD_BREACH_USERS": thresh.get("breach_users", "")
            })
            shifts = site1.get("shifts", {})
            env_map.update({
                "SHIFT_MORNING_HOURS": shifts.get("morning_hours", "06:00-14:00"),
                "SHIFT_MORNING_USERS": shifts.get("morning_users", ""),
                "SHIFT_AFTERNOON_HOURS": shifts.get("afternoon_hours", "14:00-22:00"),
                "SHIFT_AFTERNOON_USERS": shifts.get("afternoon_users", ""),
                "SHIFT_NIGHT_HOURS": shifts.get("night_hours", "22:00-06:00"),
                "SHIFT_NIGHT_USERS": shifts.get("night_users", ""),
                "SHIFT_TAG_CHANNEL": "true" if shifts.get("tag_channel") else "false",
                "SHIFT_SEND_DM": "true" if shifts.get("send_dm") else "false"
            })

            lines = []
            if os.path.exists(ENV_PATH) and os.path.isfile(ENV_PATH):
                with open(ENV_PATH, "r", encoding="utf-8") as f:
                    lines = f.readlines()

            updated = set()
            new_lines = []
            for line in lines:
                matched = False
                for k, v in env_map.items():
                    stripped = line.strip()
                    if stripped.startswith(f"{k}=") or stripped.startswith(f"{k} ="):
                        new_lines.append(f"{k}={v}\n")
                        updated.add(k)
                        matched = True
                        break
                if not matched:
                    new_lines.append(line)

            for k, v in env_map.items():
                if k not in updated:
                    new_lines.append(f"{k}={v}\n")

            with open(ENV_PATH, "w", encoding="utf-8") as f:
                f.writelines(new_lines)
        except Exception:
            pass

    @classmethod
    def get_all_sites(cls):
        cls.init()
        with cls._lock:
            res = []
            for s in cls._data.get("sites", []):
                s_copy = _make_json_safe(dict(s))
                state = cls._site_states.get(s["id"], {})
                s_copy["state"] = _make_json_safe(dict(state))
                # Mask credentials
                s_copy["grafana_password_set"] = bool(s.get("grafana_password"))
                s_copy["grafana_token_set"] = bool(s.get("grafana_token"))
                if s_copy.get("grafana_password"):
                    s_copy["grafana_password"] = "••••••••••••••••"
                if s_copy.get("grafana_token"):
                    s_copy["grafana_token"] = "••••••••••••••••"
                res.append(s_copy)
            return res

    @classmethod
    def get_site(cls, site_id):
        cls.init()
        with cls._lock:
            for s in cls._data.get("sites", []):
                if s["id"] == site_id:
                    s_copy = _make_json_safe(dict(s))
                    s_copy["state"] = _make_json_safe(dict(cls._site_states.get(site_id, {})))
                    s_copy["grafana_password_set"] = bool(s.get("grafana_password"))
                    s_copy["grafana_token_set"] = bool(s.get("grafana_token"))
                    if s_copy.get("grafana_password"):
                        s_copy["grafana_password"] = "••••••••••••••••"
                    if s_copy.get("grafana_token"):
                        s_copy["grafana_token"] = "••••••••••••••••"
                    return s_copy
            return None

    @classmethod
    def get_raw_site(cls, site_id):
        cls.init()
        with cls._lock:
            for s in cls._data.get("sites", []):
                if s["id"] == site_id:
                    return _make_json_safe(dict(s))
            return None

    @classmethod
    def get_site_lock(cls, site_id):
        cls.init()
        with cls._lock:
            if site_id not in cls._site_locks:
                cls._site_locks[site_id] = threading.Lock()
            return cls._site_locks[site_id]

    @classmethod
    def get_site_state(cls, site_id):
        cls.init()
        with cls._lock:
            if site_id not in cls._site_states:
                cls._site_states[site_id] = {
                    "is_running": False,
                    "is_paused": False,
                    "last_run_time": None,
                    "last_status": "Idle",
                    "next_run_due": time.time()
                }
            return cls._site_states[site_id]

    @classmethod
    def set_site_state(cls, site_id, **kwargs):
        cls.init()
        with cls._lock:
            if site_id not in cls._site_states:
                cls._site_states[site_id] = {
                    "is_running": False,
                    "is_paused": False,
                    "last_run_time": None,
                    "last_status": "Idle",
                    "next_run_due": time.time()
                }
            cls._site_states[site_id].update(kwargs)

    @classmethod
    def record_site_run(cls, site_id, last_run_time, last_run_ts, last_status):
        cls.init()
        with cls._lock:
            for s in cls._data.get("sites", []):
                if s["id"] == site_id:
                    s["last_run_time"] = last_run_time
                    s["last_run_ts"] = last_run_ts
                    s["last_status"] = last_status
                    break
            st = cls._site_states.get(site_id)
            if st:
                st["last_run_time"] = last_run_time
                st["last_run_ts"] = last_run_ts
                st["last_status"] = last_status
            cls._save_data_no_lock()

    @classmethod
    def get_global_settings(cls):
        cls.init()
        with cls._lock:
            return _make_json_safe(dict(cls._data.get("global_settings", cls._default_global_settings())))

    @classmethod
    def update_global_settings(cls, updates):
        cls.init()
        with cls._lock:
            g = cls._data.setdefault("global_settings", cls._default_global_settings())
            for k, v in updates.items():
                if k == "slack_bot_token" and str(v).startswith("\u2022\u2022\u2022\u2022"):
                    continue   # ignore placeholder from UI
                g[k] = v
            result = _make_json_safe(dict(g))
            cls._save_data_no_lock()
        return result

    @classmethod
    def get_timezone(cls):
        """Read timezone WITHOUT acquiring cls._lock — reads from already-initialised _data."""
        cls.init()
        # Direct read (GIL protects simple dict lookups in CPython)
        gs = cls._data.get("global_settings", {}) if cls._data else {}
        return gs.get("timezone") or "Asia/Kolkata"

    @classmethod
    def create_site(cls, name, clone_from_id=None, grafana_url=""):
        """
        Create a new site, persist it, return a safe (masked) copy for the API.
        bot_log is called AFTER the lock is released to prevent deadlock.
        """
        cls.init()
        log_msg  = ""
        safe_ret = None
        with cls._lock:
            # Build unique ID (timestamp + small counter if collision)
            new_id = f"site_{int(time.time() * 1000)}"
            while any(s["id"] == new_id for s in cls._data.get("sites", [])):
                new_id = f"site_{int(time.time() * 1000) + 1}"

            # Source: clone from existing site OR env defaults
            base = None
            if clone_from_id:
                for s in cls._data.get("sites", []):
                    if s["id"] == clone_from_id:
                        base = json.loads(json.dumps(s))   # deep copy
                        break
            if not base:
                base = cls._default_site_from_env()

            new_site = _make_json_safe(dict(base))
            new_site["id"]     = new_id
            new_site["name"]   = name.strip() or f"Site {len(cls._data['sites']) + 1}"
            new_site["paused"] = False
            if grafana_url:
                new_site["grafana_url"] = grafana_url.strip()

            cls._data["sites"].append(new_site)
            cls._site_locks[new_id]  = threading.Lock()
            cls._site_states[new_id] = {
                "is_running": False,
                "is_paused":  False,
                "last_run_time": None,
                "last_status":   "Idle",
                "next_run_due":  time.time(),
            }
            cls._save_data_no_lock()   # non-blocking Atlas push via daemon thread

            # Build safe return value while still inside lock (copy, not reference)
            safe_ret = dict(new_site)
            safe_ret["grafana_password_set"] = bool(safe_ret.get("grafana_password"))
            safe_ret["grafana_token_set"]    = bool(safe_ret.get("grafana_token"))
            if safe_ret.get("grafana_password"): safe_ret["grafana_password"] = "\u2022" * 16
            if safe_ret.get("grafana_token"):    safe_ret["grafana_token"]    = "\u2022" * 16
            safe_ret["state"] = dict(cls._site_states[new_id])

            log_msg = f"[SUCCESS] Created site '{new_site['name']}' ({new_id})"

        # bot_log AFTER lock release — it calls get_timezone() which would re-enter the lock
        bot_log(log_msg)
        return safe_ret

    @classmethod
    def update_site(cls, site_id, updates):
        """
        Apply updates dict to a site and persist.  Returns (True, safe_copy) or (False, error_str).
        bot_log called after lock release.
        """
        cls.init()
        log_msg  = ""
        safe_ret = None
        with cls._lock:
            target = next((s for s in cls._data.get("sites", []) if s["id"] == site_id), None)
            if not target:
                return False, f"Site '{site_id}' not found."

            # --- Scalar fields ---
            if "name" in updates and str(updates["name"]).strip():
                target["name"] = str(updates["name"]).strip()
            for field in ("grafana_url", "grafana_username", "slack_channel_id",
                          "slack_thread_ts", "slack_message"):
                if field in updates:
                    if field == "slack_channel_id":
                        target[field] = cls.parse_channel_id(updates[field])
                    elif field == "slack_thread_ts":
                        target[field] = cls.parse_thread_ts(updates[field])
                    else:
                        target[field] = str(updates[field]).strip()
            # Passwords — ignore placeholder dots
            for pw_field in ("grafana_password", "grafana_token"):
                if pw_field in updates:
                    v = str(updates[pw_field]).strip()
                    if not v.startswith("\u2022\u2022\u2022\u2022"):
                        target[pw_field] = v
            if "interval_minutes" in updates:
                target["interval_minutes"] = max(1, safe_int(updates["interval_minutes"], 30))
            if "paused" in updates:
                is_p = bool(updates["paused"])
                target["paused"] = is_p
                st = cls._site_states.get(site_id)
                if st:
                    st["is_paused"]   = is_p
                    st["last_status"] = "Paused" if is_p else "Idle"

            # --- Threshold ---
            thresh = target.setdefault("threshold", {})
            th_map = {
                "threshold_enabled":            ("enabled",            bool),
                "threshold_metric_type":         ("metric_type",        str),
                "threshold_operator":            ("operator",           str),
                "threshold_value":               ("value",              str),
                "threshold_keywords":            ("keywords",           str),
                "threshold_colors":              ("colors",             str),
                "threshold_breach_users":        ("breach_users",       str),
                "threshold_only_alert_on_breach":("only_alert_on_breach",bool),
            }
            for upd_key, (tgt_key, cast) in th_map.items():
                if upd_key in updates:
                    v = updates[upd_key]
                    thresh[tgt_key] = cast(v) if cast is bool else cast(v).strip()

            # --- Shifts ---
            shifts = target.setdefault("shifts", {})
            sh_map = {
                "shift_morning_hours":   ("morning_hours",    str),
                "shift_morning_users":   ("morning_users",    str),
                "shift_afternoon_hours": ("afternoon_hours",  str),
                "shift_afternoon_users": ("afternoon_users",  str),
                "shift_night_hours":     ("night_hours",      str),
                "shift_night_users":     ("night_users",      str),
                "shift_tag_channel":     ("tag_channel",      bool),
                "shift_send_dm":         ("send_dm",          bool),
            }
            for upd_key, (tgt_key, cast) in sh_map.items():
                if upd_key in updates:
                    v = updates[upd_key]
                    shifts[tgt_key] = cast(v) if cast is bool else cast(v).strip()

            cls._save_data_no_lock()

            # Build safe copy inside lock
            safe_ret = _make_json_safe(dict(target))
            safe_ret["grafana_password_set"] = bool(target.get("grafana_password"))
            safe_ret["grafana_token_set"]    = bool(target.get("grafana_token"))
            if safe_ret.get("grafana_password"): safe_ret["grafana_password"] = "\u2022" * 16
            if safe_ret.get("grafana_token"):    safe_ret["grafana_token"]    = "\u2022" * 16
            safe_ret["state"] = _make_json_safe(dict(cls._site_states.get(site_id, {})))
            log_msg = f"[SUCCESS] Updated site '{target['name']}' ({site_id})"

        bot_log(log_msg, site_id=site_id)
        return True, safe_ret

    @classmethod
    def delete_site(cls, site_id):
        """Delete a site.  Allows deleting the last site (UI handles guard)."""
        cls.init()
        log_msg  = ""
        ret_msg  = ""
        with cls._lock:
            sites = cls._data.get("sites", [])
            target_idx  = None
            site_name   = site_id
            for idx, s in enumerate(sites):
                if s["id"] == site_id:
                    target_idx = idx
                    site_name  = s.get("name", site_id)
                    break
            if target_idx is None:
                return False, f"Site '{site_id}' not found."

            cls._data["sites"].pop(target_idx)
            cls._site_locks.pop(site_id, None)
            cls._site_states.pop(site_id, None)
            cls._save_data_no_lock()
            log_msg = f"[INFO] Deleted site '{site_name}' ({site_id})"
            ret_msg = f"Site '{site_name}' deleted successfully."

        bot_log(log_msg)
        return True, ret_msg

    @classmethod
    def toggle_pause_site(cls, site_id):
        cls.init()
        log_msg  = ""
        new_val  = False
        found    = False
        with cls._lock:
            for s in cls._data.get("sites", []):
                if s["id"] == site_id:
                    new_val = not bool(s.get("paused", False))
                    s["paused"] = new_val
                    st = cls._site_states.get(site_id)
                    if st:
                        st["is_paused"]   = new_val
                        st["last_status"] = "Paused" if new_val else "Idle"
                        if new_val:
                            st["next_run_due"] = 0
                        else:
                            interval_sec = max(60, safe_int(s.get("interval_minutes"), 30) * 60)
                            st["next_run_due"] = time.time() + interval_sec
                    cls._save_data_no_lock()
                    log_msg = f"[INFO] Site '{s['name']}' ({site_id}) monitoring is now {'PAUSED' if new_val else 'STARTED (ACTIVE)'}. Other sites unaffected."
                    found = True
                    break
        if not found:
            return False, "Site not found"
        bot_log(log_msg, site_id=site_id)
        return True, new_val


    @classmethod
    def parse_channel_id(cls, raw_input):
        val = str(raw_input or "").strip()
        if not val:
            return ""
        if "<#" in val:
            m = re.search(r"<#([A-Z0-9]+)", val)
            if m:
                return m.group(1)
        if "slack.com" in val:
            if "/archives/" in val:
                after = val.split("/archives/", 1)[1].split("?")[0].strip("/")
                val = after.split("/")[0]
            elif "/client/" in val:
                after = val.split("/client/", 1)[1].split("?")[0].strip("/")
                parts = [p for p in after.split("/") if p]
                if len(parts) >= 2:
                    val = parts[1]
                elif len(parts) == 1:
                    val = parts[0]
        return re.sub(r"[<@>#\s]", "", val)

    @classmethod
    def parse_thread_ts(cls, raw_input):
        val = str(raw_input or "").strip()
        if not val:
            return ""
        if "thread_ts=" in val:
            parts = val.split("thread_ts=", 1)[1].split("&")[0].strip()
            if parts:
                return parts
        if "/thread/" in val:
            after = val.split("/thread/", 1)[1].split("?")[0].strip("/")
            if "-" in after:
                ts_part = after.split("-", 1)[1]
                if ts_part:
                    return ts_part
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


# Legacy Config adapter for CLI and backward compatibility
class Config:
    @classmethod
    def reload(cls):
        SiteManager.init()

    @classmethod
    def get_target_urls(cls):
        urls = []
        for s in SiteManager.get_all_sites():
            u = s.get("grafana_url", "").strip()
            if u and u not in urls:
                urls.append(u)
        return urls

    @classmethod
    def get_formatted_message(cls, target_url=None, title=None):
        return format_slack_message(
            template="*Grafana Snapshot Alert* - {datetime}",
            target_url=target_url,
            title=title,
            tz_name=SiteManager.get_timezone()
        )


# ==============================================================================
# IMAGE OCR & DATA EXTRACTION ENGINE
# ==============================================================================
def extract_text_via_windows_ocr(image_path):
    if sys.platform != "win32" or not image_path or not os.path.exists(image_path):
        return None

    abs_path = os.path.abspath(image_path).replace("'", "''")
    ps_cmd = f"""
    [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime] | Out-Null
    [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime] | Out-Null
    [Windows.Media.Ocr.OcrEngine, Windows.Foundation.UniversalApiContract, ContentType = WindowsRuntime] | Out-Null
    try {{
        $fOp = [Windows.Storage.StorageFile]::GetFileFromPathAsync('{abs_path}')
        while ($fOp.Status -eq 0) {{ [System.Threading.Thread]::Sleep(15) }}
        $file = $fOp.GetResults()

        $sOp = $file.OpenAsync(0)
        while ($sOp.Status -eq 0) {{ [System.Threading.Thread]::Sleep(15) }}
        $stream = $sOp.GetResults()

        $dOp = [Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)
        while ($dOp.Status -eq 0) {{ [System.Threading.Thread]::Sleep(15) }}
        $decoder = $dOp.GetResults()

        $bOp = $decoder.GetSoftwareBitmapAsync()
        while ($bOp.Status -eq 0) {{ [System.Threading.Thread]::Sleep(15) }}
        $bitmap = $bOp.GetResults()

        $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
        if ($null -eq $engine) {{
            $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage([Windows.Globalization.Language]::new('en-US'))
        }}
        if ($null -eq $engine) {{
            $engine = [Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages | Select-Object -First 1 | ForEach-Object {{ [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($_) }}
        }}

        $rOp = $engine.RecognizeAsync($bitmap)
        while ($rOp.Status -eq 0) {{ [System.Threading.Thread]::Sleep(15) }}
        $ocr = $rOp.GetResults()

        $ocr.Lines | ForEach-Object {{ $_.Text }}
    }} catch {{
        Write-Error $_
    }}
    """
    try:
        res = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-NonInteractive", "-Command", ps_cmd],
            capture_output=True,
            text=True,
            timeout=25
        )
        if res.returncode == 0 and res.stdout.strip():
            lines = [line.strip() for line in res.stdout.splitlines() if line.strip()]
            if lines:
                return lines
    except Exception:
        pass
    return None


def extract_text_via_tesseract(image_path):
    if not image_path or not os.path.exists(image_path):
        return None

    tess_bin = "tesseract"
    if sys.platform == "win32":
        candidates = [
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
            r"C:\msys64\ucrt64\bin\tesseract.exe",
            r"C:\msys64\mingw64\bin\tesseract.exe",
            r"C:\tools\tesseract\tesseract.exe",
            r"C:\ProgramData\chocolatey\bin\tesseract.exe",
        ]
        for c in candidates:
            if os.path.exists(c):
                tess_bin = c
                if pytesseract:
                    pytesseract.pytesseract.tesseract_cmd = c
                break

    if pytesseract and Image:
        try:
            img = Image.open(image_path)
            raw_text = pytesseract.image_to_string(img)
            lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
            if lines:
                return lines
        except Exception:
            pass

    try:
        res = subprocess.run([tess_bin, image_path, "stdout"], capture_output=True, text=True, timeout=20)
        if res.returncode == 0 and res.stdout.strip():
            lines = [line.strip() for line in res.stdout.splitlines() if line.strip()]
            if lines:
                return lines
    except Exception:
        pass

    return None


def analyze_image_pixels(image_path):
    if not Image or not image_path or not os.path.exists(image_path):
        return {"colors": [], "has_red": False, "has_yellow": False}
    try:
        with Image.open(image_path) as img:
            rgb_img = img.convert("RGB")
            small = rgb_img.resize((150, 150))
            pixels = list(small.getdata())
            red_count = 0
            yellow_count = 0
            total = len(pixels)
            for r, g, b in pixels:
                if r > 140 and g < 65 and b < 65:
                    red_count += 1
                elif r > 180 and g > 130 and b < 65:
                    yellow_count += 1

            detected = []
            if red_count >= (total * 0.008):
                detected.append("red")
            if yellow_count >= (total * 0.008):
                detected.append("yellow")

            return {
                "colors": detected,
                "has_red": "red" in detected,
                "has_yellow": "yellow" in detected,
                "red_pixel_ratio": round(red_count / total, 3)
            }
    except Exception:
        return {"colors": [], "has_red": False, "has_yellow": False}


def extract_image_ocr(image_path):
    if not image_path or not os.path.exists(image_path):
        return {"success": False, "engine": "none", "lines": [], "numbers": [], "raw_text": "", "pixel_colors": []}

    lines = extract_text_via_tesseract(image_path)
    engine_used = "Tesseract OCR" if lines else "none"

    if not lines and sys.platform == "win32":
        lines = extract_text_via_windows_ocr(image_path)
        if lines:
            engine_used = "Windows Native OCR"

    pixel_info = analyze_image_pixels(image_path)
    pixel_colors = pixel_info.get("colors", [])

    if not lines:
        if pixel_colors:
            return {
                "success": True,
                "engine": f"Visual Pixel Inspector ({', '.join(pixel_colors).upper()})",
                "lines": [f"Visual Color Signal: {', '.join(pixel_colors).upper()} status detected from gauge pixels."],
                "numbers": [],
                "raw_text": f"Visual Color Signal: {', '.join(pixel_colors).upper()}",
                "pixel_colors": pixel_colors
            }
        return {"success": False, "engine": "none", "lines": [], "numbers": [], "raw_text": "", "pixel_colors": []}

    raw_text = "\n".join(lines)
    numbers = []
    num_pattern = re.compile(r"([A-Za-z0-9_\-\s]{2,22}[:=]?\s*[-+]?\d+(?:\.\d+)?\s*(?:%|k|m|g|totes|orders|ms|s)?)", re.IGNORECASE)
    for line in lines:
        matches = num_pattern.findall(line)
        for m in matches:
            clean_m = m.strip()
            if clean_m and clean_m not in numbers:
                numbers.append(clean_m)

    return {
        "success": True,
        "engine": engine_used,
        "lines": lines,
        "numbers": numbers,
        "raw_text": raw_text,
        "pixel_colors": pixel_colors
    }


def extract_page_data(page):
    try:
        return page.evaluate("""() => {
            const result = {
                tables: [],
                stats: [],
                primary_metrics: [],
                badges: [],
                colors_detected: [],
                raw_text: (document.body ? document.body.innerText : "") || ""
            };

            const clean = s => (s || "").replace(/\\s+/g, " ").trim();

            const checkColor = (fg, bg, stroke, fill, cls) => {
                const combined = `${fg} ${bg} ${stroke} ${fill} ${cls}`.toLowerCase();
                if (/red|critical|danger|error|failed|#e02f44|#c4162a|#f2495c|rgb\\(2[0-5][0-9],\\s*[0-7]?[0-9],\\s*[0-7]?[0-9]\\)/i.test(combined)) {
                    if (!result.colors_detected.includes("red")) result.colors_detected.push("red");
                    return "red";
                }
                if (/yellow|warn|orange|pending|delayed|#ff9900|#faad14|#eab308|rgb\\(2[0-5][0-9],\\s*1[0-9][0-9],\\s*[0-9]+\\)/i.test(combined)) {
                    if (!result.colors_detected.includes("yellow")) result.colors_detected.push("yellow");
                    return "yellow";
                }
                if (/green|success|normal|ok|healthy|#73bf69|#52c41a|rgb\\([0-9]+,\\s*2[0-5][0-9],\\s*[0-9]+\\)/i.test(combined)) {
                    if (!result.colors_detected.includes("green")) result.colors_detected.push("green");
                    return "green";
                }
                return "normal";
            };

            // 1. Table Extraction
            const tables = document.querySelectorAll("table, [role='table'], .table-panel");
            tables.forEach((tbl) => {
                const tblData = { title: "", headers: [], rows: [] };
                const panel = tbl.closest(".panel-container, [data-testid*='panel'], .react-grid-item, .dashboard-row");
                if (panel) {
                    const titleEl = panel.querySelector(".panel-title, [data-testid*='panel-header'], h2, h3, h4");
                    if (titleEl) tblData.title = titleEl.innerText.trim();
                }

                const ths = tbl.querySelectorAll("th, [role='columnheader']");
                ths.forEach(th => {
                    const t = th.innerText.trim();
                    if (t) tblData.headers.push(t);
                });

                const trs = tbl.querySelectorAll("tbody tr, [role='row']");
                trs.forEach(tr => {
                    if (tr.querySelector("[role='columnheader']")) return;
                    const rowCells = [];
                    const tds = tr.querySelectorAll("td, [role='cell']");
                    tds.forEach(td => {
                        const text = td.innerText.trim();
                        let color = "normal";
                        const style = window.getComputedStyle(td);
                        const fg = style.color || "";
                        const bg = style.backgroundColor || "";
                        const cls = (td.className || "") + " " + (td.getAttribute("data-status") || "");

                        color = checkColor(fg, bg, "", "", cls);
                        rowCells.push({ text: text, color: color });
                    });
                    if (rowCells.length > 0) tblData.rows.push(rowCells);
                });

                tblData.row_count = tblData.rows.length;
                if (tblData.rows.length > 0 || tblData.headers.length > 0) {
                    result.tables.push(tblData);
                }
            });

            // 2. Grafana Gauges, Stat Panels & Big Numbers Deep Scan
            const panels = document.querySelectorAll(".panel-container, [data-testid*='panel'], .react-grid-item, div[class*='panel-container'], .view-panel, div[class*='panel-wrapper']");
            panels.forEach(p => {
                let title = "";
                const titleEl = p.querySelector(".panel-title, [data-testid*='panel-header'], [class*='panel-title'], h1, h2, h3, h4, header");
                if (titleEl) title = clean(titleEl.innerText);

                const svgs = p.querySelectorAll("svg");
                svgs.forEach(svg => {
                    const shapes = svg.querySelectorAll("path, circle, rect, text, tspan");
                    shapes.forEach(el => {
                        const style = window.getComputedStyle(el);
                        checkColor(style.color, style.backgroundColor, style.stroke, style.fill || el.getAttribute("fill") || "", el.getAttribute("class") || "");
                        if (el.tagName.toLowerCase() === "text" || el.tagName.toLowerCase() === "tspan") {
                            const txt = clean(el.textContent);
                            if (txt && /^[-+]?\\d+(?:\\.\\d+)?\\s*(?:s|ms|sec|seconds|%|orders|totes|units)?$/i.test(txt)) {
                                const pair = title ? `${title}: ${txt}` : txt;
                                if (!result.stats.includes(pair)) result.stats.push(pair);
                            }
                        }
                    });
                });

                const content = p.querySelector(".panel-content, div[class*='panel-content'], div[class*='panel-body']") || p;
                const els = content.querySelectorAll("div, span, p, h1, h2, h3, text, b, strong");
                els.forEach(el => {
                    if (el.children.length > 2) return;
                    const txt = clean(el.innerText || el.textContent);
                    if (!txt || txt.length > 40) return;

                    if (/^[-+]?\\d+(?:\\.\\d+)?\\s*(?:s|ms|sec|seconds|%|orders|totes|units|items|rpm|k|m|g)?$/i.test(txt)) {
                        const style = window.getComputedStyle(el);
                        checkColor(style.color, style.backgroundColor, style.stroke, style.fill, el.className);
                        const pair = title ? `${title}: ${txt}` : txt;
                        if (!result.stats.includes(pair)) result.stats.push(pair);
                        if (!result.stats.includes(txt)) result.stats.push(txt);
                        if (!result.primary_metrics.includes(pair)) result.primary_metrics.push(pair);
                    }
                });
            });

            // 3. Fallback: Parse visible body text lines
            const lines = (document.body ? document.body.innerText : "").split("\\n");
            let prevLine = "";
            for (let i = 0; i < lines.length; i++) {
                const line = clean(lines[i]);
                if (!line) continue;

                if (/^[-+]?\\d+(?:\\.\\d+)?\\s*(?:s|ms|sec|seconds|%|orders|totes|units)?$/i.test(line)) {
                    if (prevLine && prevLine.length < 50 && !/^(timeinterval|ppsid|bin_tags|all|last|greymatter|dashboard)/i.test(prevLine)) {
                        const combined = `${prevLine}: ${line}`;
                        if (!result.stats.includes(combined)) result.stats.push(combined);
                        if (!result.primary_metrics.includes(combined)) result.primary_metrics.push(combined);
                    }
                    if (!result.stats.includes(line)) result.stats.push(line);
                }

                const inlineM = line.match(/([A-Za-z0-9_\\-\\s]{3,35}[:=]\\s*[-+]?\\d+(?:\\.\\d+)?\\s*(?:s|ms|sec|seconds|%|orders|totes|units)?)/i);
                if (inlineM) {
                    const found = clean(inlineM[1]);
                    if (!result.stats.includes(found)) result.stats.push(found);
                    if (!result.primary_metrics.includes(found)) result.primary_metrics.push(found);
                }
                prevLine = line;
            }

            // 4. Badges, Status Pills & Tags
            const pills = document.querySelectorAll(".badge, [class*='status'], [class*='state'], [class*='pill'], [data-testid*='badge']");
            pills.forEach(p => {
                const t = clean(p.innerText);
                if (t && t.length < 40 && !result.badges.includes(t)) result.badges.push(t);
            });

            return result;
        }""")
    except Exception as e:
        return {"tables": [], "stats": [], "primary_metrics": [], "badges": [], "colors_detected": [], "raw_text": ""}


def evaluate_threshold(extraction_data, threshold_cfg=None):
    cfg = threshold_cfg or {}
    if not cfg.get("enabled", True):
        return {
            "breached": False,
            "status": "DISABLED",
            "summary": "Threshold monitoring disabled",
            "reasons": [],
            "total_rows": 0,
            "detected_colors": [],
            "ocr_detected": 0
        }

    metric_type = cfg.get("metric_type", "row_count") or "row_count"
    op = cfg.get("operator", ">") or ">"
    val_str = str(cfg.get("value", "0")).strip()
    raw_kw = cfg.get("keywords", "") or ""
    keywords = [k.strip().lower() for k in raw_kw.split(",") if k.strip()]
    raw_cl = cfg.get("colors", "red, orange, yellow") or "red, orange, yellow"
    colors = [c.strip().lower() for c in raw_cl.split(",") if c.strip()]

    tables = extraction_data.get("tables", [])
    raw_text = extraction_data.get("raw_text", "").lower()
    detected_colors = list(set(extraction_data.get("colors_detected", [])))
    stats = extraction_data.get("stats", [])
    primary_metrics = extraction_data.get("primary_metrics", [])
    ocr_data = extraction_data.get("ocr", {})
    ocr_raw_text = ocr_data.get("raw_text", "").lower()
    ocr_lines = ocr_data.get("lines", [])
    ocr_pixel_colors = ocr_data.get("pixel_colors", [])
    for pc in ocr_pixel_colors:
        if pc not in detected_colors:
            detected_colors.append(pc)

    total_rows = sum(t.get("row_count", 0) for t in tables)
    reasons = []
    breached = False

    all_text = f"{raw_text}\n{ocr_raw_text}".lower()

    # Check 1: Table row count
    if metric_type in ("row_count", "any"):
        try:
            target_num = float(val_str) if val_str else 0.0
            if op == ">" and total_rows > target_num:
                breached = True
                reasons.append(f"Table row count ({total_rows}) > {target_num}")
            elif op == ">=" and total_rows >= target_num:
                breached = True
                reasons.append(f"Table row count ({total_rows}) >= {target_num}")
            elif op == "<" and total_rows < target_num:
                breached = True
                reasons.append(f"Table row count ({total_rows}) < {target_num}")
            elif op == "<=" and total_rows <= target_num:
                breached = True
                reasons.append(f"Table row count ({total_rows}) <= {target_num}")
            elif op == "==" and total_rows == target_num:
                breached = True
                reasons.append(f"Table row count ({total_rows}) == {target_num}")
            elif op == "!=" and total_rows != target_num:
                breached = True
                reasons.append(f"Table row count ({total_rows}) != {target_num}")
        except ValueError:
            pass

    # Check 2: Numeric values in stats/KPIs and Image OCR
    if metric_type in ("number_val", "any"):
        try:
            target_num = float(val_str) if val_str else 0.0
            candidate_items = []
            seen = set()
            for item in (primary_metrics + stats + ocr_lines):
                c = str(item).strip()
                if c and c not in seen:
                    candidate_items.append(c)
                    seen.add(c)

            evaluated_numbers = []
            for item in candidate_items:
                found_nums = re.findall(r"[-+]?\d+(?:\.\d+)?", item)
                for fn in found_nums:
                    try:
                        n_val = float(fn)
                        evaluated_numbers.append((n_val, item))
                    except ValueError:
                        pass

            for num_val, source_line in evaluated_numbers:
                match_condition = False
                if op == ">" and num_val > target_num: match_condition = True
                elif op == ">=" and num_val >= target_num: match_condition = True
                elif op == "<" and num_val < target_num: match_condition = True
                elif op == "<=" and num_val <= target_num: match_condition = True
                elif op == "==" and num_val == target_num: match_condition = True
                elif op == "!=" and num_val != target_num: match_condition = True

                if match_condition:
                    breached = True
                    reasons.append(f"Metric value ({num_val}) {op} {target_num} in '{source_line}'")
                    break
        except Exception:
            pass

    # Check 3: Alert Keywords
    if metric_type in ("keyword", "any"):
        for kw in keywords:
            if kw in all_text:
                breached = True
                reasons.append(f"Alert keyword '{kw}' found in dashboard")

    # Check 4: Alert Colors
    if metric_type in ("color_status", "any"):
        matched_c = [c for c in colors if c in detected_colors]
        if matched_c:
            breached = True
            reasons.append(f"Detected alert status color(s): {', '.join(matched_c)}")

        for tbl in tables:
            for row in tbl.get("rows", []):
                for cell in row:
                    c_text = cell.get("text", "")
                    c_color = cell.get("color", "normal")
                    if c_color in ("red", "yellow"):
                        if not breached:
                            breached = True
                            reasons.append(f"Warning cell '{c_text}' ({c_color})")

    summary = " | ".join(reasons) if reasons else f"All clear (Rows: {total_rows})"
    return {
        "breached": breached,
        "status": "BREACHED" if breached else "NORMAL",
        "reasons": reasons,
        "summary": summary,
        "total_rows": total_rows,
        "detected_colors": detected_colors,
        "ocr_detected": len(ocr_lines)
    }


def get_active_shift(shifts_cfg=None, tz_name="Asia/Kolkata"):
    cfg = shifts_cfg or {}
    now = get_current_datetime(tz_name)
    current_min = now.hour * 60 + now.minute

    shifts = [
        ("Morning", cfg.get("morning_hours", "06:00-14:00"), cfg.get("morning_users", "")),
        ("Afternoon", cfg.get("afternoon_hours", "14:00-22:00"), cfg.get("afternoon_users", "")),
        ("Night", cfg.get("night_hours", "22:00-06:00"), cfg.get("night_users", ""))
    ]

    def _parse_time_minutes(t_str):
        try:
            parts = t_str.strip().split(":")
            return int(parts[0]) * 60 + int(parts[1])
        except Exception:
            return 0

    def _is_time_in_range(check_min, start_min, end_min):
        if start_min <= end_min:
            return start_min <= check_min < end_min
        return check_min >= start_min or check_min < end_min

    for name, hours, users in shifts:
        try:
            s_part, e_part = hours.split("-")
            s_min = _parse_time_minutes(s_part)
            e_min = _parse_time_minutes(e_part)
            if _is_time_in_range(current_min, s_min, e_min):
                u_list = [u.strip().strip("<>@") for u in (users or "").replace(",", " ").split() if u.strip()]
                return {
                    "name": name,
                    "hours": hours,
                    "users_raw": users or "",
                    "user_ids": u_list
                }
        except Exception:
            continue

    default_users = cfg.get("morning_users", "")
    u_list = [u.strip().strip("<>@") for u in (default_users or "").replace(",", " ").split() if u.strip()]
    return {
        "name": "General",
        "hours": "All-Day",
        "users_raw": default_users or "",
        "user_ids": u_list
    }


# ==============================================================================
# HEADLESS CAPTURE ENGINE
# ==============================================================================
class GrafanaCapture:
    def __init__(self, site_dict=None):
        self.site = site_dict or {}
        self.global_settings = SiteManager.get_global_settings()

    def capture_screenshot(self, target_url=None, output_path=None, username=None, password=None, token=None, return_extracted=False):
        url_to_capture = target_url or self.site.get("grafana_url")
        if not url_to_capture:
            raise ValueError("No Grafana URL configured.")

        if not output_path:
            tmp_dir = tempfile.gettempdir()
            filename = f"grafana_{self.site.get('id', 'snap')}_{int(time.time() * 1000)}.png"
            output_path = os.path.join(tmp_dir, filename)

        prepared_url = url_to_capture.strip()
        site_name = self.site.get("name", "Site")
        bot_log(f"[{site_name}] Navigating headlessly to Grafana: {prepared_url}", site_id=self.site.get("id"))

        user = username if username is not None else self.site.get("grafana_username")
        pwd = password if password is not None else self.site.get("grafana_password")
        auth_token = token if token is not None else self.site.get("grafana_token")

        extra_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        if auth_token:
            extra_headers["Authorization"] = f"Bearer {auth_token}"

        with sync_playwright() as p:
            launch_args = [
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
            launch_kwargs = {"headless": True, "args": launch_args}
            effective_proxy = get_effective_proxy(self.global_settings.get("http_proxy"))
            if effective_proxy:
                launch_kwargs["proxy"] = {"server": effective_proxy}
                bot_log(f"[{site_name}] Playwright routing via Zero-Trust Proxy: {effective_proxy}", site_id=self.site.get("id"))

            browser = p.chromium.launch(**launch_kwargs)

            viewport_w = safe_int(self.global_settings.get("viewport_width"), 1920)
            viewport_h = safe_int(self.global_settings.get("viewport_height"), 1080)
            page_wait = safe_int(self.global_settings.get("page_load_wait_seconds"), 8)

            context = browser.new_context(
                viewport={"width": viewport_w, "height": viewport_h},
                device_scale_factor=1.0,
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                extra_http_headers=extra_headers,
                ignore_https_errors=True
            )

            page = context.new_page()

            try:
                try:
                    response = page.goto(prepared_url, wait_until="domcontentloaded", timeout=30000)
                    if response and response.status >= 400:
                        bot_log(f"[{site_name}] Server returned HTTP status {response.status}", site_id=self.site.get("id"))
                except Exception as nav_err:
                    err_str = str(nav_err)
                    if "ERR_CONNECTION_REFUSED" in err_str:
                        raise ValueError(f"Connection refused at '{prepared_url}'. Verify the service is running and accessible.")
                    if "Timeout" in err_str:
                        raise TimeoutError(f"Connection timed out (30s) reaching '{prepared_url}'. The network/firewall cannot reach this server.")
                    raise RuntimeError(f"Navigation failed: {nav_err}")

                try:
                    page.wait_for_selector(
                        "input[type='password'], input[name='user'], button:has-text('Log in'), .react-grid-layout, .dashboard-container, [data-testid='dashboard-content']",
                        timeout=6000
                    )
                except Exception:
                    pass

                has_pass_input = page.locator("input[type='password'], input[name='password'], input[placeholder*='password' i]").count() > 0
                has_login_btn = page.locator("button:has-text('Log in'), button:has-text('Login'), button:has-text('Sign in'), button[type='submit']").count() > 0
                is_login = "/login" in page.url or (has_pass_input and has_login_btn)

                if is_login:
                    if user and pwd:
                        bot_log(f"[{site_name}] Detected login screen. Authenticating as '{user}'...", site_id=self.site.get("id"))
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

                        try:
                            page.wait_for_load_state("networkidle", timeout=15000)
                        except Exception:
                            pass
                        time.sleep(3)

                        try:
                            skip_btn = page.locator("button:has-text('Skip'), a:has-text('Skip')").first
                            if skip_btn.count() > 0:
                                skip_btn.click()
                                time.sleep(2)
                        except Exception:
                            pass

                        try:
                            curr_url = page.url
                            curr_path = urlparse(curr_url).path
                            prep_path = urlparse(prepared_url).path
                            if "/login" in curr_path:
                                bot_log(f"[{site_name}] Still on login screen. Please check credentials.", site_id=self.site.get("id"))
                            elif prep_path and prep_path != "/" and prep_path not in curr_path:
                                page.goto(prepared_url, wait_until="domcontentloaded", timeout=30000)
                        except Exception:
                            pass

                try:
                    page.wait_for_load_state("networkidle", timeout=12000)
                except Exception:
                    pass

                try:
                    page.wait_for_selector(".panel-loading, .loading-bar", state="hidden", timeout=8000)
                except Exception:
                    pass

                try:
                    page.wait_for_selector(".react-grid-layout, .panel-content, [data-testid*='panel'], .dashboard-container, .panel-container, [id*='panel'], table", state="visible", timeout=10000)
                except Exception:
                    pass

                time.sleep(page_wait)

                try:
                    page.add_style_tag(content="""
                        .grafana-tooltip, .portal-wrapper { display: none !important; }
                        body { overflow: hidden !important; }
                    """)
                except Exception:
                    pass

                shot_taken = False
                if "/d-solo/" in prepared_url or "viewPanel=" in prepared_url:
                    try:
                        panel = page.locator(".panel-container, .react-grid-item, .panel-content, [data-testid*='panel']").first
                        if panel.count() > 0 and panel.is_visible():
                            panel.screenshot(path=output_path)
                            shot_taken = True
                    except Exception:
                        pass

                if not shot_taken:
                    page.screenshot(path=output_path, full_page=False)

                if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
                    raise RuntimeError("Screenshot capture failed: output image was not created.")

                extracted_data = extract_page_data(page)
                ocr_res = extract_image_ocr(output_path)
                extracted_data["ocr"] = ocr_res
                if ocr_res.get("pixel_colors"):
                    for pc in ocr_res["pixel_colors"]:
                        if pc not in extracted_data["colors_detected"]:
                            extracted_data["colors_detected"].append(pc)
                if ocr_res.get("success") and ocr_res.get("numbers"):
                    for num_metric in ocr_res["numbers"]:
                        if num_metric not in extracted_data["stats"]:
                            extracted_data["stats"].append(f"[Image OCR] {num_metric}")

                file_size = os.path.getsize(output_path)
                bot_log(f"[{site_name}] Captured snapshot successfully! ({file_size / 1024:.1f} KB)", site_id=self.site.get("id"))
                if return_extracted:
                    return output_path, extracted_data
                return output_path

            finally:
                context.close()
                browser.close()

    def extract_dashboard_data(self, target_url=None, username=None, password=None, token=None):
        temp_img = os.path.join(tempfile.gettempdir(), f"extract_temp_{int(time.time() * 1000)}.png")
        try:
            _, extraction = self.capture_screenshot(
                target_url=target_url,
                output_path=temp_img,
                username=username,
                password=password,
                token=token,
                return_extracted=True
            )
            return extraction
        finally:
            if os.path.exists(temp_img):
                try:
                    os.remove(temp_img)
                except Exception:
                    pass


# ==============================================================================
# SLACK 3-STEP S3 UPLOADER
# ==============================================================================
class SlackUploader:
    def __init__(self, token=None, channel_id=None, thread_ts=None):
        self.token = token or SiteManager.get_global_settings().get("slack_bot_token")
        self.channel_id = channel_id
        self.thread_ts = thread_ts
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

    def post_text_message(self, message_text, target_channel=None):
        if not self.token:
            return False, "Slack Bot Token is missing."
        dest = target_channel or self.channel_id
        if not dest:
            return False, "Slack Channel ID is missing."
        url = "https://slack.com/api/chat.postMessage"
        payload = {"channel": dest, "text": message_text}
        if self.thread_ts:
            payload["thread_ts"] = self.thread_ts
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

    def upload_screenshot(self, image_path, message_text=None, title=None, target_channel_id=None, skip_thread=False):
        if not self.token:
            return False, "Slack Bot Token is missing."
        dest_channel = target_channel_id or self.channel_id
        if not dest_channel:
            return False, "Slack Channel ID is missing."
        if not os.path.exists(image_path):
            return False, f"File not found: {image_path}"

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
            if self.thread_ts and not skip_thread and not target_channel_id:
                complete_payload["thread_ts"] = self.thread_ts

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
                return None, "Slack Bot Token is missing the 'im:write' OAuth scope."
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


# ==============================================================================
# LOGS & EXECUTION ENGINE
# ==============================================================================
LOG_BUFFER = deque(maxlen=250)

def bot_log(message, site_id=None):
    tz = SiteManager.get_timezone()
    timestamp = get_current_datetime(tz).strftime("%H:%M:%S")
    entry = f"[{timestamp}] {message}"
    print(entry, flush=True)
    LOG_BUFFER.append({
        "timestamp": timestamp,
        "site_id": site_id or "global",
        "formatted": entry
    })


def execute_site_cycle(site_id):
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

    image_path = None
    try:
        SiteManager.set_site_state(site_id, is_running=True, last_status="Capturing...")
        grafana_url = (site.get("grafana_url") or "").strip()
        if not grafana_url:
            bot_log(f"[{site_name}] Grafana URL is empty! Please configure Grafana URL.", site_id=site_id)
            SiteManager.set_site_state(site_id, last_status="Grafana URL Missing")
            return

        bot_log(f"[{site_name}] Starting capture cycle...", site_id=site_id)

        # Acquire global semaphore so total browser processes across all sites <= 2
        with GLOBAL_CAPTURE_SEMAPHORE:
            capture = GrafanaCapture(site_dict=site)
            image_path, extraction = capture.capture_screenshot(
                target_url=grafana_url,
                output_path=None,
                username=site.get("grafana_username"),
                password=site.get("grafana_password"),
                token=site.get("grafana_token"),
                return_extracted=True
            )

        size_kb = os.path.getsize(image_path) / 1024
        threshold_cfg = site.get("threshold", {})
        shifts_cfg = site.get("shifts", {})
        tz = SiteManager.get_timezone()

        eval_res = evaluate_threshold(extraction, threshold_cfg)
        active_shift = get_active_shift(shifts_cfg, tz)

        bot_log(f"[{site_name}] Threshold: [{eval_res['status']}] - {eval_res['summary']}", site_id=site_id)
        bot_log(f"[{site_name}] Shift: {active_shift['name']} ({active_shift['hours']}) - Members: {active_shift['users_raw'] or 'None'}", site_id=site_id)

        # Silent mode: Skip alert if normal and user checked 'only alert on breach'
        if threshold_cfg.get("enabled", True) and threshold_cfg.get("only_alert_on_breach", False) and not eval_res["breached"]:
            bot_log(f"[{site_name}] Normal status. 'Only Alert on Breach' is active, skipping upload.", site_id=site_id)
            now_ts = time.time()
            now_str = get_current_time_str(tz_name=tz)
            SiteManager.record_site_run(site_id, now_str, now_ts, "Normal (Skipped)")
            return

        g_settings = SiteManager.get_global_settings()
        slack_token = g_settings.get("slack_bot_token")
        chan_id = site.get("slack_channel_id")
        thread_ts = site.get("slack_thread_ts")
        uploader = SlackUploader(token=slack_token, channel_id=chan_id, thread_ts=thread_ts) if slack_token else None

        if not uploader:
            bot_log(f"[{site_name}] Snapshot captured ({size_kb:.1f} KB), Slack upload skipped (Slack Bot Token not configured).", site_id=site_id)
            now_ts = time.time()
            now_str = get_current_time_str(tz_name=tz)
            SiteManager.record_site_run(site_id, now_str, now_ts, "Captured (No Slack)")
            return

        # Prepare alert message: strictly user-configured text + tags
        tag_mentions = ""
        all_breach_mentions = []
        if eval_res["breached"]:
            raw_breach_users = threshold_cfg.get("breach_users", "")
            breach_ids = [u.strip().strip("<>@#") for u in raw_breach_users.replace(",", " ").split() if u.strip()]
            for u in breach_ids:
                if u not in all_breach_mentions:
                    all_breach_mentions.append(u)

            if shifts_cfg.get("tag_channel", True) and active_shift["user_ids"]:
                for u in active_shift["user_ids"]:
                    if u not in all_breach_mentions:
                        all_breach_mentions.append(u)

            if all_breach_mentions:
                tag_mentions = " ".join([f"<@{u}>" for u in all_breach_mentions]) + "\n"
                bot_log(f"[{site_name}] Tagging contacts on breach: {all_breach_mentions}", site_id=site_id)
            else:
                bot_log(f"[{site_name}] No breach contacts or shift members configured to tag.", site_id=site_id)

        template_msg = site.get("slack_message") or "*Grafana Snapshot Alert* - {datetime}"
        user_body = format_slack_message(
            template_msg,
            target_url=grafana_url,
            title=site_name,
            tz_name=tz,
            trigger=eval_res.get("summary", ""),
            shift=active_shift.get("name", "")
        )
        formatted_msg = (tag_mentions + user_body).strip()

        uploaded = False
        if chan_id:
            bot_log(f"[{site_name}] Uploading snapshot to Slack channel {chan_id}...", site_id=site_id)
            ok, res = uploader.upload_screenshot(image_path, message_text=formatted_msg, title=f"{site_name} Snapshot")
            if ok:
                uploaded = True
                bot_log(f"[{site_name}] Successfully uploaded snapshot to Slack!", site_id=site_id)
            else:
                bot_log(f"[{site_name}] Slack upload error: {res}", site_id=site_id)
        else:
            bot_log(f"[{site_name}] Slack Channel ID is empty, skipping channel upload.", site_id=site_id)

        # Dispatch DM to Active Shift Members and Always-Tagged Breach Contacts if Breached
        if eval_res["breached"] and shifts_cfg.get("send_dm", True) and all_breach_mentions:
            for uid in all_breach_mentions:
                bot_log(f"[{site_name}] Dispatching DM alert to breach contact: <@{uid}>...", site_id=site_id)
                dm_msg = user_body
                dm_ok, dm_res = uploader.send_dm_snapshot(uid, image_path, message_text=dm_msg)
                if dm_ok:
                    bot_log(f"[{site_name}] DM alert delivered to <@{uid}> successfully!", site_id=site_id)
                else:
                    bot_log(f"[{site_name}] Failed sending DM to <@{uid}>: {dm_res}", site_id=site_id)

        now_ts = time.time()
        now_str = get_current_time_str(tz_name=tz)
        status_str = "Success" if uploaded else ("Captured" if not chan_id else "Upload Failed")
        SiteManager.record_site_run(
            site_id=site_id,
            last_run_time=now_str,
            last_run_ts=now_ts,
            last_status=status_str
        )

    except Exception as e:
        bot_log(f"[{site_name}] Execution error: {e}", site_id=site_id)
        SiteManager.set_site_state(site_id, last_status=f"Error: {e}")
    finally:
        if image_path and os.path.exists(image_path):
            try:
                os.remove(image_path)
            except Exception:
                pass
        SiteManager.set_site_state(site_id, is_running=False)
        site_lock.release()


def execute_cycle():
    """Trigger cycles only across active, non-paused sites."""
    sites = SiteManager.get_all_sites()
    for s in sites:
        sid = s["id"]
        state = SiteManager.get_site_state(sid)
        if s.get("enabled", True) and not s.get("paused", False) and not state.get("is_paused", False):
            t = threading.Thread(target=execute_site_cycle, args=(sid,), daemon=True)
            t.start()


# ==============================================================================
# STREAMLINED MODERN WEB UI HTML (MULTI-SITE DOCK & NEO-BRUTALIST THEME)
# ==============================================================================
HTML_TEMPLATE = r'''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>GreyOrange OpsBot — Multi-Site Operations Center</title>
  <link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 40 40'%3E%3Crect width='40' height='40' rx='8' fill='%23ff5a00'/%3E%3Cpath fill-rule='evenodd' clip-rule='evenodd' d='M20 9C14.48 9 10 13.48 10 19c0 5.52 4.48 10 10 10 3.15 0 5.95-1.45 7.8-3.75v3.25c0 3.3-3 5-7.3 5-3.3 0-6-1.3-7.3-3-.5-.7-1.5-.8-2.2-.3l-1.2.9c-.6.5-.7 1.5-.2 2.2 2.2 2.5 6 4.2 10.9 4.2 6.7 0 11.3-4 11.3-10V11c0-1.1-.9-2-2-2H20zm0 16c-3.31 0-6-2.69-6-6s2.69-6 6-6 6 2.69 6 6-2.69 6-6 6z' fill='white'/%3E%3C/svg%3E">
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

    .app-container {
      display: flex;
      height: 100vh;
      width: 100vw;
      overflow: hidden;
    }

    /* Left Sidebar: Site Switcher Dock */
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
      box-shadow: 2px 2px 0px var(--border-dark);
      border-radius: 8px;
      display: flex;
      align-items: center;
      justify-content: center;
      color: #fff;
      margin-bottom: 16px;
      cursor: pointer;
      position: relative;
    }
    .sidebar-label {
      font-size: 8px;
      font-weight: 800;
      color: #71717a;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      margin-bottom: 8px;
    }
    .site-dock {
      display: flex;
      flex-direction: column;
      gap: 10px;
      width: 100%;
      align-items: center;
      flex: 1;
      overflow-y: auto;
      overflow-x: hidden;
      padding: 4px 0;
    }
    .site-dock::-webkit-scrollbar { width: 3px; }
    .site-dock::-webkit-scrollbar-thumb { background: #27272a; border-radius: 2px; }

    .site-nav-btn {
      width: 48px;
      height: 48px;
      background: #1f1f23;
      border: 2px solid #2e2e34;
      border-radius: 6px;
      color: #a1a1aa;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      cursor: pointer;
      position: relative;
      transition: all 0.15s ease;
      font-family: 'Barlow Condensed', sans-serif;
      font-weight: 700;
      font-size: 15px;
      letter-spacing: 0.02em;
    }
    .site-nav-btn:hover {
      background: #27272a;
      color: #ffffff;
      border-color: #52525b;
    }
    .site-nav-btn.active {
      background: #27272a;
      border-color: var(--primary-orange);
      color: #ffffff;
      box-shadow: 2px 2px 0px var(--primary-orange);
    }
    .site-status-dot {
      width: 7px;
      height: 7px;
      border-radius: 50%;
      position: absolute;
      top: 4px;
      right: 4px;
      border: 1px solid #141416;
    }
    .dot-green { background: var(--accent-green); }
    .dot-amber { background: var(--accent-amber); }
    .dot-orange {
      background: var(--primary-orange);
      box-shadow: 0 0 6px var(--primary-orange);
      animation: pulse-dot 1.2s infinite ease-in-out;
    }
    .dot-red { background: var(--accent-red); }

    @keyframes pulse-dot {
      0% { transform: scale(0.9); opacity: 0.7; }
      50% { transform: scale(1.3); opacity: 1; }
      100% { transform: scale(0.9); opacity: 0.7; }
    }

    .btn-add-site {
      width: 48px;
      height: 42px;
      background: transparent;
      border: 2px dashed #3f3f46;
      border-radius: 6px;
      color: #71717a;
      display: flex;
      align-items: center;
      justify-content: center;
      cursor: pointer;
      margin-top: 6px;
      transition: all 0.15s ease;
    }
    .btn-add-site:hover {
      border-color: var(--primary-orange);
      color: var(--primary-orange);
      background: rgba(255, 90, 0, 0.08);
    }

    /* Workspace */
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
      gap: 12px;
    }
    .topbar-title {
      font-family: 'Barlow Condensed', sans-serif;
      font-size: 22px;
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
      flex-shrink: 0;
    }

    /* Stat Ribbon */
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
      grid-template-columns: 1.18fr 0.82fr;
      gap: 12px;
      padding: 6px 14px 12px;
      overflow: hidden;
    }

    /* Panel Base */
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

    .form-panel-body {
      padding: 10px 14px;
      flex: 1;
      display: flex;
      flex-direction: column;
      justify-content: flex-start;
      overflow-y: auto;
      gap: 12px;
      max-height: calc(100vh - 170px);
    }
    .form-panel-body::-webkit-scrollbar, .modal-body::-webkit-scrollbar { width: 6px; }
    .form-panel-body::-webkit-scrollbar-track, .modal-body::-webkit-scrollbar-track { background: #f1f1f4; }
    .form-panel-body::-webkit-scrollbar-thumb, .modal-body::-webkit-scrollbar-thumb { background: #d4d4d8; border-radius: 3px; }
    .form-panel-body::-webkit-scrollbar-thumb:hover, .modal-body::-webkit-scrollbar-thumb:hover { background: var(--primary-orange); }

    /* Form Fields */
    .field-row { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
    .field-row-3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 10px; }
    .form-field { display: flex; flex-direction: column; }
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

    /* Action Buttons */
    .form-actions-bar {
      margin-top: 8px;
      padding-top: 8px;
      border-top: 2px dashed var(--border-light);
      display: grid;
      grid-template-columns: repeat(5, minmax(0, 1fr));
      gap: 6px;
    }
    .form-actions-bar .btn {
      height: 34px;
      font-size: 13px;
      letter-spacing: 0.02em;
      padding: 0 6px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .btn {
      height: 36px;
      padding: 0 14px;
      border: 2px solid var(--border-dark);
      box-shadow: var(--shadow-sm);
      border-radius: 4px;
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
      white-space: nowrap;
    }
    .btn:active {
      transform: translate(1px, 1px);
      box-shadow: 1px 1px 0px var(--border-dark);
    }
    .btn-topbar {
      height: 32px;
      padding: 0 14px;
      font-size: 13.5px;
      letter-spacing: 0.03em;
    }
    .btn-icon-topbar {
      width: 32px;
      height: 32px;
      padding: 0;
      border-radius: 4px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      flex-shrink: 0;
    }
    .btn-orange {
      background: var(--primary-orange);
      color: #ffffff;
      border-color: var(--border-dark);
    }
    .btn-orange:hover { background: var(--primary-orange-hover); }
    .btn-white {
      background: #ffffff;
      color: var(--text-dark);
      border: 2px solid var(--border-dark);
    }
    .btn-white:hover { background: #f4f4f5; }
    .btn-dark {
      background: #18181b;
      color: #ffffff;
      border: 2px solid var(--border-dark);
    }
    .btn-dark:hover { background: #27272a; }
    .btn-danger-outline {
      background: #ffffff;
      color: var(--accent-red);
      border: 2px solid var(--border-dark);
      box-shadow: var(--shadow-sm);
    }
    .btn-danger-outline:hover {
      background: #fee2e2;
      color: #b91c1c;
      border-color: var(--border-dark);
    }

    /* Console Terminal */
    .console-body {
      background: #09090b;
      color: #e4e4e7;
      font-family: 'JetBrains Mono', monospace;
      font-size: 11.5px;
      line-height: 1.55;
      padding: 12px;
      flex: 1;
      overflow-y: auto;
      display: flex;
      flex-direction: column;
      gap: 2px;
    }
    .console-body::-webkit-scrollbar { width: 6px; }
    .console-body::-webkit-scrollbar-thumb { background: #27272a; border-radius: 3px; }
    .log-info { color: #93c5fd; }
    .log-success { color: #86efac; }
    .log-warn { color: #fde047; }
    .log-error { color: #fca5a5; }

    /* Modals */
    .modal-backdrop {
      position: fixed;
      top: 0; left: 0; width: 100vw; height: 100vh;
      background: rgba(0, 0, 0, 0.65);
      backdrop-filter: blur(2px);
      z-index: 999;
      display: none;
      align-items: center;
      justify-content: center;
      padding: 16px;
    }
    .modal-box {
      background: #ffffff;
      border: 3px solid var(--border-dark);
      box-shadow: 6px 6px 0px var(--border-dark);
      border-radius: 6px;
      max-width: 600px;
      width: 90vw;
      max-height: 85vh;
      display: flex;
      flex-direction: column;
      overflow: hidden;
      animation: modal-pop 0.15s ease-out;
    }
    .modal-box-large { max-width: 900px; }
    @keyframes modal-pop {
      from { transform: scale(0.95); opacity: 0; }
      to { transform: scale(1); opacity: 1; }
    }
    .modal-header {
      background: #faf8f5;
      border-bottom: 2px solid var(--border-dark);
      padding: 10px 16px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      font-family: 'Barlow Condensed', sans-serif;
      font-size: 18px;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: 0.03em;
    }
    .modal-body {
      padding: 14px 16px;
      overflow-y: auto;
      flex: 1;
    }
    .modal-footer {
      background: #faf8f5;
      border-top: 2px solid var(--border-dark);
      padding: 10px 16px;
      display: flex;
      justify-content: flex-end;
      gap: 10px;
    }

    .toast {
      position: fixed;
      bottom: 20px;
      right: 20px;
      background: #18181b;
      color: #fff;
      padding: 10px 16px;
      border: 2px solid var(--border-dark);
      box-shadow: var(--shadow-sm);
      border-radius: 4px;
      font-size: 13px;
      font-weight: 600;
      z-index: 1000;
      display: none;
      align-items: center;
      gap: 8px;
      border-left: 5px solid var(--primary-orange);
    }

    /* Site-Specific Monitoring Control Card */
    .monitoring-control-card {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 10px 14px;
      border: 2px solid var(--border-dark);
      border-radius: 4px;
      box-shadow: var(--shadow-sm);
      transition: all 0.2s ease;
    }
    .monitoring-control-card.active-state {
      background: #f0fdf4;
      border-color: #16a34a;
    }
    .monitoring-control-card.paused-state {
      background: #fffbeb;
      border-color: #d97706;
    }
    .status-indicator-circle {
      width: 14px;
      height: 14px;
      border-radius: 50%;
      flex-shrink: 0;
      display: inline-block;
    }
    .status-indicator-circle.active {
      background: #16a34a;
      box-shadow: 0 0 0 3px rgba(22, 163, 74, 0.25);
    }
    .status-indicator-circle.paused {
      background: #d97706;
      box-shadow: 0 0 0 3px rgba(217, 119, 6, 0.25);
    }
    .status-indicator-circle.capturing {
      background: #ff5a00;
      box-shadow: 0 0 0 3px rgba(255, 90, 0, 0.35);
      animation: pulse-ring 1.2s infinite;
    }
    @keyframes pulse-ring {
      0% { transform: scale(0.95); opacity: 0.8; }
      50% { transform: scale(1.15); opacity: 1; }
      100% { transform: scale(0.95); opacity: 0.8; }
    }
  </style>
</head>
<body>

<div class="app-container">
  <!-- Left Rail: Site Switcher Dock -->
  <aside class="sidebar">
    <div class="sidebar-logo" onclick="openGlobalConfigModal()" title="GreyOrange OpsBot — Click for Global Settings">
      <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
        <rect x="3" y="11" width="18" height="10" rx="2"></rect>
        <circle cx="12" cy="5" r="2"></circle>
        <path d="M12 7v4"></path>
        <line x1="8" y1="16" x2="8" y2="16"></line>
        <line x1="16" y1="16" x2="16" y2="16"></line>
      </svg>
    </div>

    <div class="sidebar-label">SITES</div>

    <div class="site-dock" id="site-dock-list">
      <!-- Injected dynamically via loadSites() -->
    </div>

    <button type="button" class="btn-add-site" onclick="openAddSiteModal()" title="Add New Monitoring Site">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
        <line x1="12" y1="5" x2="12" y2="19"></line>
        <line x1="5" y1="12" x2="19" y2="12"></line>
      </svg>
    </button>
  </aside>

  <!-- Main Workspace -->
  <main class="workspace">
    <!-- Topbar -->
    <header class="topbar">
      <div class="topbar-left">
        <div class="topbar-title">
          <span style="color: var(--primary-orange);">SITE:</span>
          <span id="topbar-site-name">Loading...</span>
        </div>
        <span id="topbar-site-status" class="metric-pill pill-green">ONLINE</span>
        <span id="topbar-site-interval" class="topbar-tag">EVERY 30 MINS</span>
        <span id="topbar-site-channel" class="topbar-tag">#CHANNEL</span>
      </div>

      <div class="topbar-right">
        <button type="button" class="btn btn-white btn-topbar" onclick="openGlobalConfigModal()" title="Global Settings (Slack Bot Token, Proxy, Timezone)">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
            <circle cx="12" cy="12" r="3"></circle>
            <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"></path>
          </svg>
          <span>Global Settings</span>
        </button>

        <button type="button" class="btn btn-dark btn-topbar" onclick="triggerCurrentSiteRun()" id="btn-topbar-run" title="Capture and Upload Now">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="var(--primary-orange)" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polygon points="5 3 19 12 5 21 5 3"></polygon></svg>
          Run Now
        </button>

        <button type="button" class="btn btn-danger-outline btn-icon-topbar" onclick="openDeleteModal()" id="btn-delete-site" title="Delete this Site">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path></svg>
        </button>
      </div>
    </header>

    <!-- Metrics Ribbon -->
    <section class="metrics-ribbon">
      <div class="metric-card">
        <div class="metric-info">
          <span class="metric-label">MONITORED SITES</span>
          <span class="metric-value" id="metric-total-sites">1 ACTIVE</span>
        </div>
        <span class="metric-pill pill-slate">MULTI-SITE</span>
      </div>
      <div class="metric-card">
        <div class="metric-info">
          <span class="metric-label">CADENCE (INTERVAL)</span>
          <span class="metric-value" id="metric-interval">30 MINS</span>
        </div>
        <span class="metric-pill pill-slate">RECURRING</span>
      </div>
      <div class="metric-card">
        <div class="metric-info">
          <span class="metric-label">ACTIVE SITE STATUS</span>
          <span class="metric-value" id="metric-last-status">ONLINE</span>
        </div>
        <span class="metric-pill pill-green" id="status-pill">ACTIVE</span>
      </div>
      <div class="metric-card">
        <div class="metric-info">
          <span class="metric-label">LAST EXECUTION</span>
          <span class="metric-value" id="metric-last-time" style="font-size: 16px;">PENDING</span>
        </div>
        <span class="metric-pill pill-slate">UTC/IST</span>
      </div>
    </section>

    <!-- Main Grid: Form Left, Console Right -->
    <div class="hero-grid">
      <!-- Left Panel: Site Settings Form -->
      <section class="panel">
        <div class="panel-header">
          <div class="panel-title">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"></circle><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"></path></svg>
            <span id="form-site-title">SITE SETTINGS: SITE 1 (PRIMARY)</span>
          </div>
          <span class="badge-subtle badge-orange-tag" id="active-site-id-badge">ID: SITE_1</span>
        </div>

        <form class="form-panel-body" id="site-settings-form" onsubmit="event.preventDefault(); saveCurrentSite();">
          <!-- Active Site Monitoring State & Quick Action Card -->
          <div id="site-monitoring-banner" class="monitoring-control-card">
            <div style="display: flex; align-items: center; gap: 12px;">
              <div id="site-status-indicator-dot" class="status-indicator-circle active"></div>
              <div>
                <div style="font-family: 'Barlow Condensed', sans-serif; font-size: 16px; font-weight: 800; letter-spacing: 0.5px; text-transform: uppercase;" id="site-status-headline">MONITORING ACTIVELY RUNNING</div>
                <div style="font-size: 11px; color: var(--text-dim);" id="site-status-subline">Captures execute automatically every 30 minutes for this site.</div>
              </div>
            </div>
            <button type="button" class="btn btn-white" id="btn-pause-toggle" onclick="toggleCurrentSitePause()" style="font-size: 12px; font-weight: 700; padding: 6px 14px; display: inline-flex; align-items: center; gap: 6px;">
              <span id="pause-icon" style="display: inline-flex; align-items: center;">
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><rect x="6" y="4" width="4" height="16"></rect><rect x="14" y="4" width="4" height="16"></rect></svg>
              </span>
              <span id="pause-btn-text">Pause Monitoring</span>
            </button>
          </div>

          <!-- Site Name & Cadence -->
          <div class="field-row" style="grid-template-columns: 1.8fr 1fr;">
            <div class="form-field">
              <label class="form-label" for="site_name">Site Operations Name</label>
              <input type="text" id="site_name" class="input-field" placeholder="e.g. Atlanta Distribution Hub" required oninput="document.getElementById('topbar-site-name').innerText = this.value.toUpperCase()">
            </div>
            <div class="form-field">
              <label class="form-label" for="interval_minutes">Cadence (Minutes)</label>
              <input type="number" id="interval_minutes" class="input-field input-code" value="30" min="1" max="1440">
            </div>
          </div>

          <!-- Grafana URL -->
          <div class="form-field">
            <label class="form-label" for="grafana_url">
              <span>Grafana Target URL</span>
              <span class="badge-subtle">Dashboard or Solo Panel</span>
            </label>
            <input type="text" id="grafana_url" class="input-field input-code" placeholder="http://grafana-host:3000/d/... or /d-solo/..." required>
          </div>

          <!-- Grafana Credentials -->
          <div class="field-row">
            <div class="form-field">
              <label class="form-label" for="grafana_username">Grafana Username</label>
              <input type="text" id="grafana_username" class="input-field input-code" placeholder="Leave empty if not required">
            </div>
            <div class="form-field">
              <label class="form-label" for="grafana_password">Grafana Password</label>
              <div class="input-box">
                <input type="password" id="grafana_password" class="input-field input-code" placeholder="••••••••">
                <button type="button" class="input-eye-btn" onclick="toggleVisibility('grafana_password')">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"></path><circle cx="12" cy="12" r="3"></circle></svg>
                </button>
              </div>
            </div>
          </div>

          <!-- Slack Destination -->
          <div style="background: #fafaf9; border: 1.5px solid #e4e4e7; border-radius: 4px; padding: 10px; margin-bottom: 12px;">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
              <label class="form-label" style="margin-bottom: 0; font-size: 11px; color: var(--primary-orange); display: flex; align-items: center; gap: 5px;">
                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"></path></svg>
                Slack Workspace Connection
              </label>
              <button type="button" class="btn btn-white" style="height: 22px; font-size: 10px; padding: 0 8px;" onclick="openGlobalConfigModal()">
                ⚙️ View / Edit Slack Bot Token
              </button>
            </div>
            <div style="font-size: 11px; color: #52525b; display: flex; align-items: center; justify-content: space-between; background: #fff; padding: 6px 8px; border: 1px solid #e4e4e7; border-radius: 3px; margin-bottom: 10px;">
              <span>Slack Bot OAuth Token (Global):</span>
              <span style="font-family: monospace; font-size: 11px; color: var(--accent-green); font-weight: 700;">● Active (xoxb-...)</span>
            </div>

            <div class="field-row">
              <div class="form-field">
                <label class="form-label" for="slack_channel_id">
                  <span>Target Slack Channel ID</span>
                  <span class="badge-subtle">Starts with C</span>
                </label>
                <input type="text" id="slack_channel_id" class="input-field input-code" placeholder="C060WECJEBZ" required>
              </div>
              <div class="form-field">
                <label class="form-label" for="slack_thread_ts">
                  <span>Thread Timestamp</span>
                  <span class="badge-subtle">Optional</span>
                </label>
                <input type="text" id="slack_thread_ts" class="input-field input-code" placeholder="1783950460.340209">
              </div>
            </div>

            <div class="form-field" style="margin-top: 8px;">
              <label class="form-label" for="slack_message">Slack Notification Message Template</label>
              <input type="text" id="slack_message" class="input-field input-code" value="*Grafana Snapshot Alert* - {datetime}">
            </div>
          </div>

          <!-- Threshold Engine Accordion/Section -->
          <div style="background: #fafaf9; border: 1.5px solid #e4e4e7; border-radius: 4px; padding: 10px;">
            <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px;">
              <label class="form-label" style="margin-bottom: 0; font-size: 11px; color: var(--primary-orange);">
                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"></polygon></svg>
                Threshold & Anomaly Trigger Engine
              </label>
              <label style="display: flex; align-items: center; gap: 6px; font-size: 11px; font-weight: 700; color: #52525b; cursor: pointer;">
                <input type="checkbox" id="threshold_enabled" checked style="accent-color: var(--primary-orange);"> Enable Evaluation
              </label>
            </div>

            <div class="field-row-3">
              <div class="form-field">
                <label class="form-label" for="threshold_metric_type">Metric Type</label>
                <select id="threshold_metric_type" class="input-field">
                  <option value="row_count">Table Row Count</option>
                  <option value="number_val">Numeric KPI / Gauge</option>
                  <option value="color_status">Status Color (Red/Yellow)</option>
                  <option value="keyword">Alert Keyword Match</option>
                  <option value="any">Any Anomaly (All Checks)</option>
                </select>
              </div>
              <div class="form-field">
                <label class="form-label" for="threshold_operator">Operator</label>
                <select id="threshold_operator" class="input-field">
                  <option value=">">&gt; (Greater than)</option>
                  <option value=">=">&gt;= (Greater or equal)</option>
                  <option value="<">&lt; (Less than)</option>
                  <option value="<=">&lt;= (Less or equal)</option>
                  <option value="==">== (Equals)</option>
                  <option value="!=">!= (Not equals)</option>
                </select>
              </div>
              <div class="form-field">
                <label class="form-label" for="threshold_value">Target Value</label>
                <input type="text" id="threshold_value" class="input-field input-code" value="0">
              </div>
            </div>

            <!-- Dynamic Alert Keywords & Trigger Status Colors Fields -->
            <div class="field-row" style="margin-top: 8px;">
              <div class="form-field">
                <label class="form-label" for="threshold_keywords">
                  <span>Alert Keywords (comma-separated)</span>
                  <span class="badge-subtle badge-orange-tag">Matches Text in Dashboard</span>
                </label>
                <input type="text" id="threshold_keywords" class="input-field input-code" placeholder="e.g. error, critical, breach, failed, high">
              </div>
              <div class="form-field">
                <label class="form-label" for="threshold_colors">
                  <span>Trigger Status Colors (comma-separated)</span>
                  <span class="badge-subtle">Cell / Metric Colors</span>
                </label>
                <input type="text" id="threshold_colors" class="input-field input-code" placeholder="red, orange, yellow" value="red, orange, yellow">
              </div>
            </div>

            <div class="form-field" style="margin-top: 8px;">
              <label class="form-label" for="threshold_breach_users">
                <span>Always Tag People on Breach (Slack Member IDs)</span>
                <span class="badge-subtle badge-orange-tag">Tagged on Every Breach</span>
              </label>
              <input type="text" id="threshold_breach_users" class="input-field input-code" placeholder="e.g. U01234567, U08765432">
            </div>

            <div style="margin-top: 6px;">
              <label style="display: flex; align-items: center; gap: 6px; font-size: 11px; font-weight: 700; color: #52525b; cursor: pointer;">
                <input type="checkbox" id="threshold_only_alert_on_breach" style="accent-color: var(--primary-orange);"> Only alert & dispatch DM when threshold is breached (Silent when normal)
              </label>
            </div>
          </div>

          <!-- Shift Schedule Section -->
          <div style="background: #fafaf9; border: 1.5px solid #e4e4e7; border-radius: 4px; padding: 10px;">
            <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px;">
              <label class="form-label" style="margin-bottom: 0; font-size: 11px; color: var(--primary-orange);">
                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"></circle><polyline points="12 6 12 12 16 14"></polyline></svg>
                Shift Schedule (3 Shifts)
              </label>
              <span class="badge-subtle badge-orange-tag" id="active-shift-badge">DETECTING ACTIVE SHIFT...</span>
            </div>

            <!-- Morning -->
            <div class="field-row" style="grid-template-columns: 110px 1fr; margin-bottom: 6px;">
              <div class="form-field">
                <label class="form-label" for="shift_morning_hours">Morning</label>
                <input type="text" id="shift_morning_hours" class="input-field input-code" value="06:00-14:00">
              </div>
              <div class="form-field">
                <label class="form-label" for="shift_morning_users">Slack Member IDs (starts with U)</label>
                <input type="text" id="shift_morning_users" class="input-field input-code" placeholder="e.g. U01234567, U09876543">
              </div>
            </div>

            <!-- Afternoon -->
            <div class="field-row" style="grid-template-columns: 110px 1fr; margin-bottom: 6px;">
              <div class="form-field">
                <label class="form-label" for="shift_afternoon_hours">Afternoon</label>
                <input type="text" id="shift_afternoon_hours" class="input-field input-code" value="14:00-22:00">
              </div>
              <div class="form-field">
                <label class="form-label" for="shift_afternoon_users">Slack Member IDs (starts with U)</label>
                <input type="text" id="shift_afternoon_users" class="input-field input-code" placeholder="e.g. U02345678">
              </div>
            </div>

            <!-- Night -->
            <div class="field-row" style="grid-template-columns: 110px 1fr; margin-bottom: 6px;">
              <div class="form-field">
                <label class="form-label" for="shift_night_hours">Night</label>
                <input type="text" id="shift_night_hours" class="input-field input-code" value="22:00-06:00">
              </div>
              <div class="form-field">
                <label class="form-label" for="shift_night_users">Slack Member IDs (starts with U)</label>
                <input type="text" id="shift_night_users" class="input-field input-code" placeholder="e.g. U03456789">
              </div>
            </div>

            <div class="field-row" style="margin-top: 6px;">
              <label style="display: flex; align-items: center; gap: 6px; font-size: 11px; font-weight: 700; color: #52525b; cursor: pointer;">
                <input type="checkbox" id="shift_tag_channel" checked style="accent-color: var(--primary-orange);"> Tag on-duty members in Channel
              </label>
              <label style="display: flex; align-items: center; gap: 6px; font-size: 11px; font-weight: 700; color: #52525b; cursor: pointer;">
                <input type="checkbox" id="shift_send_dm" checked style="accent-color: var(--primary-orange);"> Send Alert & Snapshot to their DM
              </label>
            </div>
          </div>

          <!-- Bottom Action Buttons -->
          <div class="form-actions-bar">
            <button type="submit" class="btn btn-orange" id="btn-save" title="Save Site Settings">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"></path><polyline points="17 21 17 13 7 13 7 21"></polyline><polyline points="7 3 7 8 15 8"></polyline></svg>
              Save Site
            </button>
            <button type="button" class="btn btn-white" onclick="previewCurrentSiteData()" id="btn-extract-preview" title="Extract and Inspect Active Site Dashboard Text & Thresholds">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 2a14.5 14.5 0 0 0 0 20 14.5 14.5 0 0 0 0-20"/><path d="M2 12h20"/></svg>
              Data Preview
            </button>
            <button type="button" class="btn btn-white" onclick="previewCurrentSiteSnapshot()" id="btn-preview" title="Headless Screenshot Preview of Active Site">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"></path><circle cx="12" cy="13" r="4"></circle></svg>
              Snapshot
            </button>
            <button type="button" class="btn btn-white" onclick="testCurrentSiteSlack()" id="btn-slack" title="Test Slack Connectivity for this Site">
              <svg width="13" height="13" viewBox="0 0 128 128"><path d="M26.2 78.4a12.8 12.8 0 1 1-12.8-12.8h12.8v12.8zm6.5 0a12.8 12.8 0 0 1 25.6 0v32.2a12.8 12.8 0 1 1-25.6 0V78.4z" fill="#E01E5A"/><path d="M49.6 26.2a12.8 12.8 0 1 1 12.8-12.8v12.8H49.6zm0 6.5a12.8 12.8 0 0 1 0 25.6H17.4a12.8 12.8 0 1 1 0-25.6h32.2z" fill="#36C5F0"/><path d="M101.8 49.6a12.8 12.8 0 1 1 12.8 12.8h-12.8V49.6zm-6.5 0a12.8 12.8 0 0 1-25.6 0V17.4a12.8 12.8 0 1 1 25.6 0v32.2z" fill="#2EB67D"/><path d="M78.4 101.8a12.8 12.8 0 1 1-12.8 12.8v-12.8h12.8zm0-6.5a12.8 12.8 0 0 1 0-25.6h32.2a12.8 12.8 0 1 1 0 25.6H78.4z" fill="#ECB22E"/></svg>
              Ping Channel
            </button>
            <button type="button" class="btn btn-white" onclick="testCurrentSiteDM()" id="btn-dm" title="Send a Test DM to your Slack Member ID">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
              Test DM
            </button>
            <button type="button" class="btn btn-dark" onclick="triggerCurrentSiteRun()" id="btn-run" title="Capture and Upload Now for this Site">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="var(--primary-orange)" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polygon points="5 3 19 12 5 21 5 3"></polygon></svg>
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
          <div style="display: flex; gap: 8px; align-items: center;">
            <select id="log-site-filter" class="input-field" style="height: 24px; font-size: 11px; padding: 0 4px; width: 140px;" onchange="fetchLogs()">
              <option value="all">All Sites Stream</option>
            </select>
            <button type="button" class="btn btn-white" style="height: 24px; font-size: 11px; padding: 0 8px; box-shadow: none;" onclick="clearLogs()">Clear</button>
            <button type="button" class="btn btn-white" style="height: 24px; font-size: 11px; padding: 0 8px; box-shadow: none;" onclick="fetchLogs(true)">Refresh</button>
          </div>
        </div>

        <div class="console-body" id="log-output">
          <div>[System] GreyOrange Multi-Site Operations Center Initialized...</div>
        </div>
      </section>
    </div>
  </main>
</div>

<!-- Add Site Modal -->
<div class="modal-backdrop" id="add-site-modal" onclick="closeAddSiteModal()">
  <div class="modal-box" onclick="event.stopPropagation()">
    <div class="modal-header">
      <span>Add New Monitoring Site</span>
      <button type="button" class="btn btn-white" style="height: 26px; padding: 0 10px; font-size: 11px;" onclick="closeAddSiteModal()">Close</button>
    </div>
    <div class="modal-body">
      <div class="form-field" style="margin-bottom: 12px;">
        <label class="form-label" for="new-site-name">Site Operations Name</label>
        <input type="text" id="new-site-name" class="input-field" placeholder="e.g. Dallas Distribution Hub, Site 2" required>
      </div>
      <div class="form-field" style="margin-bottom: 12px;">
        <label class="form-label" for="new-site-url">Grafana Dashboard URL (Optional)</label>
        <input type="text" id="new-site-url" class="input-field input-code" placeholder="http://grafana-host:3000/d/...">
      </div>
      <div style="margin-top: 8px;">
        <label style="display: flex; align-items: center; gap: 6px; font-size: 12px; font-weight: 600; color: #27272a; cursor: pointer;">
          <input type="checkbox" id="new-site-clone-creds" checked style="accent-color: var(--primary-orange);"> Copy Slack Channel, Credentials & Shifts from active site
        </label>
      </div>
    </div>
    <div class="modal-footer">
      <button type="button" class="btn btn-white" onclick="closeAddSiteModal()">Cancel</button>
      <button type="button" class="btn btn-orange" onclick="submitCreateSite()">Create Site</button>
    </div>
  </div>
</div>

<!-- Delete Site Confirmation Modal -->
<div class="modal-backdrop" id="delete-site-modal" onclick="closeDeleteModal()">
  <div class="modal-box" style="max-width: 420px;" onclick="event.stopPropagation()">
    <div class="modal-header">
      <span style="color: var(--accent-red);">Confirm Site Deletion</span>
      <button type="button" class="btn btn-white" style="height: 26px; padding: 0 10px; font-size: 11px;" onclick="closeDeleteModal()">Close</button>
    </div>
    <div class="modal-body">
      <p style="font-size: 13px; line-height: 1.5; color: #3f3f46;">
        Are you sure you want to delete <strong id="delete-site-name" style="color: #18181b;">this site</strong>?
      </p>
      <p style="font-size: 12px; color: var(--accent-red); margin-top: 8px; font-weight: 600;">
        This will stop all monitoring for this site and remove its configuration. This action cannot be undone.
      </p>
    </div>
    <div class="modal-footer">
      <button type="button" class="btn btn-white" onclick="closeDeleteModal()">Cancel</button>
      <button type="button" class="btn btn-danger-outline" style="background: var(--accent-red); color: #fff; border-color: var(--accent-red);" onclick="submitDeleteSite()">Yes, Delete Site</button>
    </div>
  </div>
</div>

<!-- Global Settings Modal -->
<div class="modal-backdrop" id="global-config-modal" onclick="closeGlobalConfigModal()">
  <div class="modal-box" onclick="event.stopPropagation()">
    <div class="modal-header">
      <span>Global Hub Settings</span>
      <button type="button" class="btn btn-white" style="height: 26px; padding: 0 10px; font-size: 11px;" onclick="closeGlobalConfigModal()">Close</button>
    </div>
    <div class="modal-body">
      <div class="form-field" style="margin-bottom: 12px;">
        <label class="form-label" for="global_slack_bot_token">
          <span>Global Slack Bot Token</span>
          <span class="badge-subtle">Shared by all sites</span>
        </label>
        <div class="input-box">
          <input type="password" id="global_slack_bot_token" class="input-field input-code" placeholder="xoxb-••••••••••••••••">
          <button type="button" class="input-eye-btn" onclick="toggleVisibility('global_slack_bot_token')">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"></path><circle cx="12" cy="12" r="3"></circle></svg>
          </button>
        </div>
      </div>
      <div class="field-row" style="margin-bottom: 12px;">
        <div class="form-field">
          <label class="form-label" for="global_timezone">Timezone</label>
          <input type="text" id="global_timezone" class="input-field input-code" value="Asia/Kolkata">
        </div>
        <div class="form-field">
          <label class="form-label" for="global_proxy">HTTP/SOCKS Proxy (VM only)</label>
          <input type="text" id="global_proxy" class="input-field input-code" placeholder="Leave empty on Windows">
        </div>
      </div>
    </div>
    <div class="modal-footer">
      <button type="button" class="btn btn-white" onclick="closeGlobalConfigModal()">Cancel</button>
      <button type="button" class="btn btn-orange" onclick="saveGlobalConfig()">Save Global Settings</button>
    </div>
  </div>
</div>

<!-- Preview Modal -->
<div class="modal-backdrop" id="preview-modal" onclick="closeModal()">
  <div class="modal-box" onclick="event.stopPropagation()">
    <div class="modal-header">
      <div style="display: flex; align-items: center; gap: 8px;">
        <div style="width: 22px; height: 22px; background: var(--primary-orange); border-radius: 4px; display: flex; align-items: center; justify-content: center;">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"></path><circle cx="12" cy="13" r="4"></circle></svg>
        </div>
        <span id="preview-modal-title">Headless Capture Preview</span>
      </div>
      <button type="button" class="btn btn-white" style="height: 26px; padding: 0 10px; font-size: 11px;" onclick="closeModal()">Close</button>
    </div>
    <div class="modal-body" id="modal-body"></div>
  </div>
</div>

<!-- Extracted Text & Threshold Modal -->
<div class="modal-backdrop" id="extract-modal" onclick="closeExtractModal()">
  <div class="modal-box modal-box-large" onclick="event.stopPropagation()">
    <div class="modal-header">
      <div style="display: flex; align-items: center; gap: 8px;">
        <div style="width: 22px; height: 22px; background: var(--primary-orange); border-radius: 4px; display: flex; align-items: center; justify-content: center;">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 2a14.5 14.5 0 0 0 0 20 14.5 14.5 0 0 0 0-20"/><path d="M2 12h20"/></svg>
        </div>
        <span id="extract-modal-title">Extracted Dashboard Data & Threshold Inspector</span>
      </div>
      <button type="button" class="btn btn-white" style="height: 26px; padding: 0 10px; font-size: 11px;" onclick="closeExtractModal()">Close</button>
    </div>
    <div class="modal-body" id="extract-modal-body" style="text-align: left; padding: 18px;"></div>
  </div>
</div>

<div class="toast" id="toast">Notification</div>

<script>
  let ALL_SITES = [];
  let CURRENT_SITE_ID = null;

  function showToast(msg, isError = false) {
    const t = document.getElementById("toast");
    t.innerText = msg;
    t.style.borderLeftColor = isError ? "var(--accent-red)" : "var(--primary-orange)";
    t.style.display = "flex";
    if (window._toastTimer) clearTimeout(window._toastTimer);
    window._toastTimer = setTimeout(() => { t.style.display = "none"; }, isError ? 8000 : 4000);
  }

  function toggleVisibility(id) {
    const el = document.getElementById(id);
    if (!el) return;
    el.type = el.type === "password" ? "text" : "password";
  }

  function clearLogs() {
    document.getElementById("log-output").innerHTML = '<div style="color: #71717a;">[Log cleared]</div>';
  }

  function getSiteInitials(name) {
    if (!name) return "S";
    const parts = name.replace(/[:_\-]/g, " ").trim().split(/\s+/);
    if (parts.length >= 2) {
      return (parts[0][0] + parts[1][0]).toUpperCase();
    }
    return name.substring(0, 2).toUpperCase();
  }

  async function loadSites(keepActive = true) {
    try {
      const res = await fetch("/api/sites");
      const data = await res.json();

      ALL_SITES = data.sites || [];

      // Update site count metric
      const count = ALL_SITES.length;
      document.getElementById("metric-total-sites").innerText =
        count === 0 ? "0 SITES" :
        count === 1 ? "1 SITE ACTIVE" : count + " SITES ACTIVE";

      // Update Site Filter Dropdown in Console
      const filterSelect = document.getElementById("log-site-filter");
      const currentFilter = filterSelect.value;
      filterSelect.innerHTML = '<option value="all">All Sites Stream</option>';
      ALL_SITES.forEach(s => {
        const opt = document.createElement("option");
        opt.value = s.id;
        opt.innerText = s.name;
        filterSelect.appendChild(opt);
      });
      if (currentFilter) filterSelect.value = currentFilter;

      // Select default site if needed
      if (ALL_SITES.length > 0) {
        if (!CURRENT_SITE_ID || !ALL_SITES.some(s => s.id === CURRENT_SITE_ID)) {
          CURRENT_SITE_ID = ALL_SITES[0].id;
        }
      } else {
        CURRENT_SITE_ID = null;
      }

      renderSiteDock();

      if (ALL_SITES.length > 0) {
        if (!keepActive || !document.getElementById("site_name").value) {
          populateActiveSite();
        } else {
          updateActiveSiteStatusBadge();
        }
      } else {
        // No sites yet — show blank state
        document.getElementById("topbar-site-name").innerText = "NO SITE SELECTED";
        document.getElementById("form-site-title").innerText = "SITE SETTINGS";
        document.getElementById("active-site-id-badge").innerText = "NO SITES YET";
      }
    } catch(e) {
      console.error("loadSites error:", e);
    }
  }

  function renderSiteDock() {
    const dock = document.getElementById("site-dock-list");
    dock.innerHTML = "";

    if (ALL_SITES.length === 0) {
      dock.innerHTML = '<div style="color:#3f3f46; font-size:9px; text-align:center; padding:8px 4px; line-height:1.4;">No sites.<br>Click + to add.</div>';
      return;
    }

    ALL_SITES.forEach(s => {
      const btn = document.createElement("div");
      btn.className = "site-nav-btn " + (s.id === CURRENT_SITE_ID ? "active" : "");
      btn.onclick = () => switchSite(s.id);

      const state = s.state || {};
      const isPaused = s.paused || state.is_paused;
      let dotClass = isPaused ? "dot-amber" : "dot-green";
      if (state.is_running) dotClass = "dot-orange";
      if (state.last_status && state.last_status.toLowerCase().includes("error")) dotClass = "dot-red";

      const statusDesc = isPaused ? "PAUSED (STANDBY)" : (state.is_running ? "RUNNING" : "MONITORING ACTIVE");
      btn.title = `${s.name} [${statusDesc}]`;

      btn.innerHTML = `
        <div class="site-status-dot ${dotClass}"></div>
        <span>${getSiteInitials(s.name)}</span>
        <span style="font-size: 8px; font-weight: 800; color: ${isPaused ? '#d97706' : '#16a34a'}; line-height: 1;">${isPaused ? 'OFF' : 'ON'}</span>
      `;
      dock.appendChild(btn);
    });
  }

  function switchSite(siteId) {
    CURRENT_SITE_ID = siteId;
    renderSiteDock();
    populateActiveSite();
    fetchLogs();
  }

  function populateActiveSite() {
    const site = ALL_SITES.find(s => s.id === CURRENT_SITE_ID);
    if (!site) return;

    document.getElementById("topbar-site-name").innerText = site.name.toUpperCase();
    document.getElementById("form-site-title").innerText = "SITE SETTINGS: " + site.name.toUpperCase();
    document.getElementById("active-site-id-badge").innerText = "ID: " + site.id.toUpperCase();
    document.getElementById("topbar-site-interval").innerText = "EVERY " + (site.interval_minutes || 30) + " MINS";
    document.getElementById("topbar-site-channel").innerText = site.slack_channel_id ? "#" + site.slack_channel_id : "#NO-CHANNEL";
    document.getElementById("metric-interval").innerText = (site.interval_minutes || 30) + " MINS";

    // Form inputs
    document.getElementById("site_name").value = site.name || "";
    document.getElementById("interval_minutes").value = site.interval_minutes || 30;
    document.getElementById("grafana_url").value = site.grafana_url || "";
    document.getElementById("grafana_username").value = site.grafana_username || "";
    document.getElementById("grafana_password").value = site.grafana_password_set ? "••••••••••••••••" : "";
    const tokEl = document.getElementById("grafana_token");
    if (tokEl) tokEl.value = site.grafana_token_set ? "••••••••••••••••" : "";
    document.getElementById("slack_channel_id").value = site.slack_channel_id || "";
    document.getElementById("slack_thread_ts").value = site.slack_thread_ts || "";
    document.getElementById("slack_message").value = site.slack_message || "*Grafana Snapshot Alert* - {datetime}";

    // Thresholds
    const t = site.threshold || {};
    document.getElementById("threshold_enabled").checked = t.enabled !== false;
    document.getElementById("threshold_metric_type").value = t.metric_type || "row_count";
    document.getElementById("threshold_operator").value = t.operator || ">";
    document.getElementById("threshold_value").value = t.value !== undefined ? t.value : "0";
    document.getElementById("threshold_keywords").value = t.keywords || "";
    document.getElementById("threshold_colors").value = t.colors || "red, orange, yellow";
    document.getElementById("threshold_breach_users").value = t.breach_users || "";
    document.getElementById("threshold_only_alert_on_breach").checked = !!t.only_alert_on_breach;

    // Shifts
    const sh = site.shifts || {};
    document.getElementById("shift_morning_hours").value = sh.morning_hours || "06:00-14:00";
    document.getElementById("shift_morning_users").value = sh.morning_users || "";
    document.getElementById("shift_afternoon_hours").value = sh.afternoon_hours || "14:00-22:00";
    document.getElementById("shift_afternoon_users").value = sh.afternoon_users || "";
    document.getElementById("shift_night_hours").value = sh.night_hours || "22:00-06:00";
    document.getElementById("shift_night_users").value = sh.night_users || "";
    document.getElementById("shift_tag_channel").checked = sh.tag_channel !== false;
    document.getElementById("shift_send_dm").checked = sh.send_dm !== false;

    updateActiveSiteStatusBadge();
  }

  function updateActiveSiteStatusBadge() {
    const site = ALL_SITES.find(s => s.id === CURRENT_SITE_ID);
    if (!site) return;

    const state = site.state || {};
    const topStatus = document.getElementById("topbar-site-status");
    const lastStat = document.getElementById("metric-last-status");
    const lastTime = document.getElementById("metric-last-time");
    const pill = document.getElementById("status-pill");
    const pauseBtnText = document.getElementById("pause-btn-text");
    const pauseIcon = document.getElementById("pause-icon");
    const pauseBtn = document.getElementById("btn-pause-toggle");

    const banner = document.getElementById("site-monitoring-banner");
    const bannerDot = document.getElementById("site-status-indicator-dot");
    const bannerHeadline = document.getElementById("site-status-headline");
    const bannerSubline = document.getElementById("site-status-subline");

    if (state.last_status) lastStat.innerText = state.last_status.toUpperCase();
    if (state.last_run_time) lastTime.innerText = state.last_run_time;

    const intervalMin = site.interval_minutes || 30;
    const isPaused = site.paused || state.is_paused;

    if (isPaused) {
      pauseBtnText.innerText = "Start Monitoring";
      pauseIcon.innerHTML = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polygon points="5 3 19 12 5 21 5 3"></polygon></svg>';
      if (pauseBtn) {
        pauseBtn.className = "btn btn-orange";
      }
      if (banner) {
        banner.className = "monitoring-control-card paused-state";
      }
      if (bannerDot) {
        bannerDot.className = "status-indicator-circle paused";
      }
      if (bannerHeadline) {
        bannerHeadline.innerText = "MONITORING STOPPED (PAUSED)";
      }
      if (bannerSubline) {
        bannerSubline.innerText = `This site is paused. Automated background checks every ${intervalMin}m are suspended.`;
      }
      topStatus.innerText = "PAUSED (STANDBY)";
      topStatus.className = "metric-pill pill-amber";
      pill.innerText = "PAUSED";
      pill.className = "metric-pill pill-amber";
    } else {
      pauseBtnText.innerText = "Pause Monitoring";
      pauseIcon.innerHTML = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><rect x="6" y="4" width="4" height="16"></rect><rect x="14" y="4" width="4" height="16"></rect></svg>';
      if (pauseBtn) {
        pauseBtn.className = "btn btn-white";
      }
      if (banner) {
        banner.className = "monitoring-control-card active-state";
      }
      if (bannerDot) {
        bannerDot.className = state.is_running ? "status-indicator-circle capturing" : "status-indicator-circle active";
      }
      if (bannerHeadline) {
        bannerHeadline.innerText = state.is_running ? "CAPTURING LIVE SNAPSHOT NOW..." : "MONITORING ACTIVELY RUNNING";
      }
      if (bannerSubline) {
        bannerSubline.innerText = `Automated captures execute recurringly every ${intervalMin} minutes for this site.`;
      }
      if (state.is_running) {
        topStatus.innerText = "CAPTURING";
        topStatus.className = "metric-pill pill-orange";
        pill.innerText = "BUSY";
        pill.className = "metric-pill pill-orange";
      } else {
        topStatus.innerText = "MONITORING ACTIVE";
        topStatus.className = "metric-pill pill-green";
        pill.innerText = "ACTIVE";
        pill.className = "metric-pill pill-green";
      }
    }
  }

  async function toggleCurrentSitePause() {
    if (!CURRENT_SITE_ID) return;
    try {
      const res = await fetch(`/api/sites/${CURRENT_SITE_ID}/toggle-pause`, { method: "POST" });
      const data = await res.json();
      if (data.success) {
        showToast(data.is_paused ? "Monitoring PAUSED for this site (other sites unaffected)" : "Monitoring STARTED for this site (other sites unaffected)");
        await loadSites(true);
      }
    } catch(e) {
      showToast("Failed to toggle site monitoring state", true);
    }
  }

  async function saveCurrentSite(silent = false) {
    if (!CURRENT_SITE_ID) return false;
    const btn = document.getElementById("btn-save");
    let orig = "";
    if (!silent && btn) {
      btn.disabled = true;
      orig = btn.innerHTML;
      btn.innerHTML = "Saving...";
    }

    const payload = {
      name: document.getElementById("site_name").value.trim(),
      interval_minutes: document.getElementById("interval_minutes").value,
      grafana_url: document.getElementById("grafana_url").value.trim(),
      grafana_username: document.getElementById("grafana_username").value.trim(),
      grafana_password: document.getElementById("grafana_password").value.trim(),
      grafana_token: document.getElementById("grafana_token") ? document.getElementById("grafana_token").value.trim() : (site.grafana_token || ""),
      slack_channel_id: document.getElementById("slack_channel_id").value.trim(),
      slack_thread_ts: document.getElementById("slack_thread_ts").value.trim(),
      slack_message: document.getElementById("slack_message").value.trim(),
      threshold_enabled: document.getElementById("threshold_enabled").checked,
      threshold_metric_type: document.getElementById("threshold_metric_type").value,
      threshold_operator: document.getElementById("threshold_operator").value,
      threshold_value: document.getElementById("threshold_value").value.trim(),
      threshold_keywords: document.getElementById("threshold_keywords").value.trim(),
      threshold_colors: document.getElementById("threshold_colors").value.trim(),
      threshold_breach_users: document.getElementById("threshold_breach_users").value.trim(),
      threshold_only_alert_on_breach: document.getElementById("threshold_only_alert_on_breach").checked,
      shift_morning_hours: document.getElementById("shift_morning_hours").value.trim(),
      shift_morning_users: document.getElementById("shift_morning_users").value.trim(),
      shift_afternoon_hours: document.getElementById("shift_afternoon_hours").value.trim(),
      shift_afternoon_users: document.getElementById("shift_afternoon_users").value.trim(),
      shift_night_hours: document.getElementById("shift_night_hours").value.trim(),
      shift_night_users: document.getElementById("shift_night_users").value.trim(),
      shift_tag_channel: document.getElementById("shift_tag_channel").checked,
      shift_send_dm: document.getElementById("shift_send_dm").checked
    };

    try {
      const res = await fetch(`/api/sites/${CURRENT_SITE_ID}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: json_stringify_safe(payload)
      });
      const data = await res.json();
      if (data.success) {
        if (!silent) showToast("Site settings saved successfully!");
        await loadSites(true);
        return true;
      } else {
        showToast("Save failed: " + (data.error || "Unknown error"), true);
        return false;
      }
    } catch(e) {
      if (!silent) showToast("Network error while saving site", true);
      return false;
    } finally {
      if (!silent && btn) {
        btn.disabled = false;
        btn.innerHTML = orig;
      }
    }
  }

  function json_stringify_safe(obj) {
    return JSON.stringify(obj);
  }

  async function triggerCurrentSiteRun() {
    if (!CURRENT_SITE_ID) return;
    // Auto-save form inputs first so any edited channel/credentials are applied
    await saveCurrentSite(true);

    const btn = document.getElementById("btn-run");
    btn.disabled = true;
    const orig = btn.innerHTML;
    btn.innerHTML = "Running...";
    try {
      const res = await fetch(`/api/sites/${CURRENT_SITE_ID}/trigger`, { method: "POST" });
      const data = await res.json();
      if (data.success) showToast("Capture cycle triggered for active site!");
      else showToast("Run failed: " + data.error, true);
    } catch(e) {
      showToast("Unable to trigger execution", true);
    } finally {
      setTimeout(() => { btn.disabled = false; btn.innerHTML = orig; }, 2500);
      loadSites(true);
    }
  }

  async function testCurrentSiteSlack() {
    if (!CURRENT_SITE_ID) return;
    // Auto-save form inputs first so newly entered channel is tested
    await saveCurrentSite(true);

    const btn = document.getElementById("btn-slack");
    btn.disabled = true;
    const orig = btn.innerHTML;
    btn.innerHTML = "Pinging...";
    try {
      const res = await fetch(`/api/sites/${CURRENT_SITE_ID}/test-slack`, { method: "POST" });
      const data = await res.json();
      if (data.success) showToast(data.message);
      else showToast("Slack Error: " + data.error, true);
    } catch(e) {
      showToast("Slack ping failed", true);
    } finally {
      btn.disabled = false;
      btn.innerHTML = orig;
      fetchLogs();
    }
  }

  async function testCurrentSiteDM() {
    if (!CURRENT_SITE_ID) return;
    // Auto-save form inputs first
    await saveCurrentSite(true);
    const rawBreach = document.getElementById("threshold_breach_users").value || "";
    const rawShift = document.getElementById("shift_morning_users").value || "";
    const firstSuggested = (rawBreach || rawShift).replace(/[,]/g, " ").trim().split(/\s+/)[0] || "";

    const memberId = prompt(
      "Enter your Slack Member ID to receive an in-person test DM:\n(Click your Slack profile avatar -> Click '...' -> Click 'Copy member ID')",
      firstSuggested
    );
    if (!memberId || !memberId.trim()) return;

    const btn = document.getElementById("btn-dm");
    btn.disabled = true;
    const orig = btn.innerHTML;
    btn.innerHTML = "Sending DM...";
    try {
      const res = await fetch(`/api/sites/${CURRENT_SITE_ID}/test-dm`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user_id: memberId.trim() })
      });
      const data = await res.json();
      if (data.success) {
        showToast(data.message);
      } else {
        showToast("DM Error: " + data.error, true);
      }
    } catch(e) {
      showToast("Network error testing DM", true);
    } finally {
      btn.disabled = false;
      btn.innerHTML = orig;
      fetchLogs();
    }
  }

  async function previewCurrentSiteSnapshot() {
    if (!CURRENT_SITE_ID) return;
    const btn = document.getElementById("btn-preview");
    btn.disabled = true;
    const orig = btn.innerHTML;
    btn.innerHTML = "Capturing...";
    const m = document.getElementById("preview-modal");
    const mb = document.getElementById("modal-body");
    m.style.display = "flex";
    mb.innerHTML = '<div style="padding: 30px; text-align: center; color: #71717a; font-weight: 600;">Running headless browser capture for active site... (takes ~6-10s)</div>';

    try {
      const res = await fetch(`/api/sites/${CURRENT_SITE_ID}/preview-capture`, { method: "POST" });
      const data = await res.json();
      if (data.success && data.image_url) {
        mb.innerHTML = `<img src="${data.image_url}?t=${Date.now()}" style="max-width: 100%; border: 2px solid var(--border-dark); border-radius: 4px; box-shadow: var(--shadow-sm);" alt="Grafana Preview">`;
        showToast("Snapshot captured successfully!");
      } else {
        mb.innerHTML = `<div style="padding: 20px; color: var(--accent-red); font-weight: 600;">Failed: ${data.error || "Unknown error"}</div>`;
        showToast("Snapshot failed", true);
      }
    } catch(e) {
      mb.innerHTML = `<div style="padding: 20px; color: var(--accent-red);">Network error while capturing snapshot.</div>`;
    } finally {
      btn.disabled = false;
      btn.innerHTML = orig;
      fetchLogs();
    }
  }

  async function previewCurrentSiteData() {
    if (!CURRENT_SITE_ID) return;
    const btn = document.getElementById("btn-extract-preview");
    btn.disabled = true;
    const orig = btn.innerHTML;
    btn.innerHTML = "Extracting...";
    const m = document.getElementById("extract-modal");
    const mb = document.getElementById("extract-modal-body");
    m.style.display = "flex";
    mb.innerHTML = '<div style="padding: 30px; text-align: center; color: #71717a; font-weight: 600;">Extracting dashboard tables, gauges & running OCR... (takes ~6-10s)</div>';

    try {
      const res = await fetch(`/api/sites/${CURRENT_SITE_ID}/preview-extracted-text`, { method: "POST" });
      const data = await res.json();
      if (data.success) {
        const ev = data.evaluation || {};
        const ext = data.extraction || {};
        const shift = data.active_shift || {};

        let statusBadge = ev.breached ? '<span class="cell-tag tag-red">THRESHOLD BREACHED</span>' : '<span class="cell-tag tag-green">NORMAL / ALL CLEAR</span>';

        let html = `
          <div style="margin-bottom: 14px; padding: 12px; border: 2px solid var(--border-dark); border-radius: 4px; background: #faf8f5;">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px;">
              <strong style="font-family: 'Barlow Condensed'; font-size: 18px;">THRESHOLD EVALUATION: ${statusBadge}</strong>
              <span style="font-size: 11px; font-weight: 700;">Shift: ${shift.name || 'General'} (${shift.hours || 'All-Day'})</span>
            </div>
            <div style="font-size: 13px; color: #27272a;">${ev.summary || 'No trigger condition met.'}</div>
          </div>
        `;

        if (ext.stats && ext.stats.length) {
          html += `<div style="margin-bottom: 12px;"><strong>Detected Metrics / Gauge Values:</strong><div style="display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px;">`;
          ext.stats.forEach(st => {
            html += `<span style="background: #f4f4f5; border: 1px solid #d4d4d8; padding: 3px 8px; border-radius: 3px; font-size: 11px; font-family: monospace;">${st}</span>`;
          });
          html += `</div></div>`;
        }

        if (ext.tables && ext.tables.length) {
          html += `<strong>Dashboard Tables:</strong>`;
          ext.tables.forEach((tbl, idx) => {
            html += `<div style="margin-top: 8px; margin-bottom: 12px;"><div style="font-size: 12px; font-weight: 700; color: #52525b;">Table ${idx+1}: ${tbl.title || 'Untitled'} (${tbl.row_count || 0} rows)</div>`;
            if (tbl.rows && tbl.rows.length) {
              html += `<table class="data-table-preview">`;
              if (tbl.headers && tbl.headers.length) {
                html += `<tr>${tbl.headers.map(h => `<th>${h}</th>`).join('')}</tr>`;
              }
              tbl.rows.slice(0, 5).forEach(r => {
                html += `<tr>${r.map(c => `<td>${c.text}</td>`).join('')}</tr>`;
              });
              html += `</table>`;
            }
            html += `</div>`;
          });
        }

        mb.innerHTML = html;
        showToast("Dashboard extracted successfully!");
      } else {
        mb.innerHTML = `<div style="color: var(--accent-red);">Extraction failed: ${data.error}</div>`;
      }
    } catch(e) {
      mb.innerHTML = `<div style="color: var(--accent-red);">Network error while extracting dashboard.</div>`;
    } finally {
      btn.disabled = false;
      btn.innerHTML = orig;
      fetchLogs();
    }
  }

  function closeModal() { document.getElementById("preview-modal").style.display = "none"; }
  function closeExtractModal() { document.getElementById("extract-modal").style.display = "none"; }

  // Add Site Modal Handlers
  function openAddSiteModal() {
    document.getElementById("new-site-name").value = "";
    document.getElementById("new-site-url").value = "";
    document.getElementById("add-site-modal").style.display = "flex";
    setTimeout(() => document.getElementById("new-site-name").focus(), 50);
  }
  function closeAddSiteModal() { document.getElementById("add-site-modal").style.display = "none"; }

  async function submitCreateSite() {
    const name = document.getElementById("new-site-name").value.trim();
    if (!name) {
      showToast("Please enter a site name", true);
      return;
    }
    const url = document.getElementById("new-site-url").value.trim();
    const clone = document.getElementById("new-site-clone-creds").checked;
    // Only clone from active site if sites actually exist
    const cloneId = (clone && CURRENT_SITE_ID && ALL_SITES.length > 0) ? CURRENT_SITE_ID : null;

    const btn = document.querySelector('#add-site-modal .btn-orange');
    const origText = btn.innerText;
    btn.disabled = true;
    btn.innerText = "Creating...";

    try {
      const res = await fetch("/api/sites", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: name,
          grafana_url: url,
          clone_from_id: cloneId
        })
      });
      const data = await res.json();
      if (data.success && data.site) {
        closeAddSiteModal();
        showToast(`Site '${data.site.name}' created successfully!`);
        CURRENT_SITE_ID = data.site.id;
        await loadSites(false);   // reload and switch to new site
        populateActiveSite();     // ensure form fills with new site data
      } else {
        showToast("Error: " + (data.error || "Failed to create site"), true);
      }
    } catch(e) {
      showToast("Network error creating site", true);
    } finally {
      btn.disabled = false;
      btn.innerText = origText;
    }
  }

  // Delete Site Modal Handlers
  function openDeleteModal() {
    if (ALL_SITES.length <= 1) {
      showToast("Cannot delete the only remaining site.", true);
      return;
    }
    const site = ALL_SITES.find(s => s.id === CURRENT_SITE_ID);
    if (site) {
      document.getElementById("delete-site-name").innerText = `'${site.name}'`;
    }
    document.getElementById("delete-site-modal").style.display = "flex";
  }
  function closeDeleteModal() { document.getElementById("delete-site-modal").style.display = "none"; }

  async function submitDeleteSite() {
    if (!CURRENT_SITE_ID) return;
    try {
      const res = await fetch(`/api/sites/${CURRENT_SITE_ID}`, { method: "DELETE" });
      const data = await res.json();
      if (data.success) {
        closeDeleteModal();
        showToast("Site deleted successfully!");
        CURRENT_SITE_ID = null;
        await loadSites(false);
      } else {
        showToast("Delete failed: " + data.error, true);
      }
    } catch(e) {
      showToast("Error deleting site", true);
    }
  }

  // Global Config Modal
  async function openGlobalConfigModal() {
    try {
      const res = await fetch("/api/global-settings");
      const data = await res.json();
      if (data.settings) {
        document.getElementById("global_timezone").value = data.settings.timezone || "Asia/Kolkata";
        document.getElementById("global_proxy").value = data.settings.http_proxy || "";
        document.getElementById("global_slack_bot_token").value = data.settings.slack_bot_token || "";
      }
    } catch(e) {}
    document.getElementById("global-config-modal").style.display = "flex";
  }
  function closeGlobalConfigModal() { document.getElementById("global-config-modal").style.display = "none"; }

  async function saveGlobalConfig() {
    const payload = {
      timezone: document.getElementById("global_timezone").value.trim(),
      http_proxy: document.getElementById("global_proxy").value.trim(),
      slack_bot_token: document.getElementById("global_slack_bot_token").value.trim()
    };
    try {
      const res = await fetch("/api/global-settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: json_stringify_safe(payload)
      });
      const data = await res.json();
      if (data.success) {
        showToast("Global settings saved!");
        closeGlobalConfigModal();
      } else {
        showToast("Failed to save global settings", true);
      }
    } catch(e) {
      showToast("Network error saving global settings", true);
    }
  }

  async function fetchLogs(animate = false) {
    try {
      const filterSite = document.getElementById("log-site-filter").value;
      const url = filterSite && filterSite !== "all" ? `/api/logs?site_id=${filterSite}` : "/api/logs";
      const res = await fetch(url);
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
    await loadSites(true);
    await fetchLogs();
  }

  // Boot: poll until MongoDB is ready and sites are loaded (up to 20s)
  async function bootPollUntilReady() {
    await loadSites(false);
    await fetchLogs();
    // If no sites yet, keep retrying every 1.5s for up to 20s (waiting for Mongo background thread)
    let retries = 0;
    const maxRetries = 14;
    const retryInterval = setInterval(async () => {
      if (ALL_SITES.length > 0 || retries >= maxRetries) {
        clearInterval(retryInterval);
        return;
      }
      retries++;
      await loadSites(false);
    }, 1500);
  }

  // Initial Boot
  bootPollUntilReady();
  setInterval(poll, 3500);
</script>

</body>
</html>
'''


# ==============================================================================
# FLASK WEB SERVER & API ENDPOINTS
# ==============================================================================
app = Flask(__name__)


@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)


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
        data = request.get_json() or {}
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
        print(f"[ERROR] create_site exception: {e}", flush=True)
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
        data = request.get_json() or {}
        ok, res = SiteManager.update_site(site_id, data)
        if not ok:
            return jsonify({"success": False, "error": res}), 400
        return jsonify({"success": True, "site": res})
    except Exception as e:
        print(f"[ERROR] update_site exception: {e}", flush=True)
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/sites/<site_id>", methods=["DELETE"])
def delete_single_site(site_id):
    try:
        ok, msg = SiteManager.delete_site(site_id)
        if not ok:
            return jsonify({"success": False, "error": msg}), 400
        return jsonify({"success": True, "message": msg})
    except Exception as e:
        print(f"[ERROR] delete_site exception: {e}", flush=True)
        return jsonify({"success": False, "error": str(e)}), 500



@app.route("/api/sites/<site_id>/toggle-pause", methods=["POST"])
def toggle_site_pause(site_id):
    ok, is_p = SiteManager.toggle_pause_site(site_id)
    if not ok:
        return jsonify({"success": False, "error": is_p}), 400
    return jsonify({"success": True, "is_paused": is_p})


@app.route("/api/sites/<site_id>/trigger", methods=["POST"])
def trigger_site_run(site_id):
    site = SiteManager.get_raw_site(site_id)
    if not site:
        return jsonify({"success": False, "error": "Site not found"}), 404

    t = threading.Thread(target=execute_site_cycle, args=(site_id,), daemon=True)
    t.start()
    return jsonify({"success": True, "message": f"Capture cycle triggered for {site.get('name', site_id)}."})


@app.route("/api/sites/<site_id>/preview-capture", methods=["POST"])
def preview_site_capture(site_id):
    site = SiteManager.get_raw_site(site_id)
    if not site:
        return jsonify({"success": False, "error": "Site not found"}), 404

    url = site.get("grafana_url")
    if not url:
        return jsonify({"success": False, "error": "Grafana URL is empty for this site."}), 400

    cleanup_old_previews()
    bot_log(f"[INFO] Headless snapshot preview request for site '{site.get('name')}'...", site_id=site_id)

    try:
        capture = GrafanaCapture(site_dict=site)
        preview_filename = f"preview_{site_id}_{int(time.time() * 1000)}.png"
        preview_path = os.path.join(tempfile.gettempdir(), preview_filename)

        with GLOBAL_CAPTURE_SEMAPHORE:
            capture.capture_screenshot(
                target_url=url,
                output_path=preview_path,
                username=site.get("grafana_username"),
                password=site.get("grafana_password"),
                token=site.get("grafana_token")
            )
        size_kb = os.path.getsize(preview_path) / 1024
        return jsonify({
            "success": True,
            "image_url": f"/api/preview-image/{preview_filename}",
            "message": f"Successfully captured snapshot ({size_kb:.1f} KB)"
        })
    except Exception as e:
        bot_log(f"[ERROR] [{site.get('name')}] Preview capture failed: {e}", site_id=site_id)
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/sites/<site_id>/preview-extracted-text", methods=["POST"])
def preview_site_extracted_text(site_id):
    site = SiteManager.get_raw_site(site_id)
    if not site:
        return jsonify({"success": False, "error": "Site not found"}), 404

    url = site.get("grafana_url")
    if not url:
        return jsonify({"success": False, "error": "Grafana URL is empty for this site."}), 400

    bot_log(f"[INFO] Dashboard data extraction request for site '{site.get('name')}'...", site_id=site_id)
    try:
        capture = GrafanaCapture(site_dict=site)
        with GLOBAL_CAPTURE_SEMAPHORE:
            extraction = capture.extract_dashboard_data(
                target_url=url,
                username=site.get("grafana_username"),
                password=site.get("grafana_password"),
                token=site.get("grafana_token")
            )
        eval_res = evaluate_threshold(extraction, site.get("threshold", {}))
        active_shift = get_active_shift(site.get("shifts", {}), SiteManager.get_timezone())

        return jsonify({
            "success": True,
            "extraction": extraction,
            "evaluation": eval_res,
            "active_shift": active_shift
        })
    except Exception as e:
        bot_log(f"[ERROR] [{site.get('name')}] Extraction failed: {e}", site_id=site_id)
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/sites/<site_id>/test-slack", methods=["POST"])
def test_site_slack(site_id):
    site = SiteManager.get_raw_site(site_id)
    if not site:
        return jsonify({"success": False, "error": "Site not found"}), 404

    g_settings = SiteManager.get_global_settings()
    token = g_settings.get("slack_bot_token")
    chan = site.get("slack_channel_id")
    thread_ts = site.get("slack_thread_ts")

    uploader = SlackUploader(token=token, channel_id=chan, thread_ts=thread_ts)
    ok, details = uploader.test_auth()
    if not ok:
        return jsonify({"success": False, "error": str(details)}), 400

    user = details.get("user", "Bot")
    team = details.get("team", "Workspace")

    if not chan:
        return jsonify({
            "success": True,
            "message": f"Connected to '{team}' as '{user}'! (Slack Channel ID is empty for this site, so ping was not posted to a channel)."
        })

    post_ok, post_res = uploader.post_text_message(
        f"*Slack Test Ping Successful for {site.get('name')}*\nConnected as `{user}` on `{team}` at {get_current_time_str(tz_name=SiteManager.get_timezone())}."
    )
    if post_ok:
        return jsonify({
            "success": True,
            "message": f"Connected to '{team}' as '{user}'! Test ping posted to channel {chan}."
        })
    return jsonify({
        "success": False,
        "error": f"Connected as '{user}' on '{team}', but posting to channel {chan} failed: {post_res}"
    }), 400


@app.route("/api/sites/<site_id>/test-dm", methods=["POST"])
def test_site_dm(site_id):
    site = SiteManager.get_raw_site(site_id)
    if not site:
        return jsonify({"success": False, "error": "Site not found"}), 404

    data = request.get_json() or {}
    target_user = str(data.get("user_id") or "").strip()

    if not target_user:
        threshold_cfg = site.get("threshold", {})
        shifts_cfg = site.get("shifts", {})
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
    site_name = site.get("name", site_id)
    now_str = get_current_time_str(tz_name=SiteManager.get_timezone())

    test_message = (
        f"*[OpsBot Direct Message Alert Test]*\n"
        f"Hello! This is an in-person test DM sent by *{bot_user}* on *{team}*.\n"
        f"• *Target Site:* {site_name}\n"
        f"• *Timestamp:* {now_str}\n"
        f"• *Delivery:* Direct Message (1-on-1)\n"
        f"Your in-person Slack DM alerting is active and working properly!"
    )

    dm_ok, dm_res = uploader.send_dm_text(target_user, test_message)
    if dm_ok:
        bot_log(f"[{site_name}] Test DM delivered successfully to <@{target_user}>!", site_id=site_id)
        return jsonify({
            "success": True,
            "message": f"Test DM sent to <@{target_user}>! Check your Slack Direct Messages."
        })
    return jsonify({
        "success": False,
        "error": f"Failed sending DM to <@{target_user}>: {dm_res}"
    }), 400


@app.route("/api/global-settings", methods=["GET", "POST"])
def handle_global_settings():
    if request.method == "POST":
        data = request.get_json() or {}
        updated = SiteManager.update_global_settings(data)
        return jsonify({"success": True, "settings": updated})

    g = SiteManager.get_global_settings()
    actual_tok = g.get("slack_bot_token", "")
    g["slack_bot_token_set"] = bool(actual_tok)
    g["slack_bot_token"] = actual_tok
    return jsonify({"success": True, "settings": g})


@app.route("/api/logs", methods=["GET"])
def get_logs():
    filter_site = request.args.get("site_id")
    if filter_site and filter_site != "all":
        logs = [l["formatted"] for l in LOG_BUFFER if l.get("site_id") == filter_site or l.get("site_id") == "global"]
    else:
        logs = [l["formatted"] for l in LOG_BUFFER]
    return jsonify({"logs": logs})


# Backward compatibility endpoints for any legacy calls
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
    base_dir = os.path.dirname(os.path.abspath(__file__))
    target_png = os.path.join(base_dir, "app_icon.png")
    if os.path.exists(target_png) and os.path.getsize(target_png) > 1000:
        return target_png

    gen_path = r"C:\Users\aditya.s.ctr\.gemini\antigravity-ide\brain\603f260b-cf24-426c-b4b2-6104ecb1f7f4\bot_app_logo_1790899095532.jpg"
    if os.path.exists(gen_path):
        try:
            from PIL import Image
            with Image.open(gen_path) as im:
                im.save(target_png, "PNG")
            return target_png
        except Exception:
            pass

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


# ==============================================================================
# ZERO-CONFLICT SCHEDULER & ENTRYPOINT
# ==============================================================================
def scheduler_loop():
    time.sleep(3)
    bot_log("[INFO] Multi-Site Zero-Conflict Concurrency Scheduler resumed.")
    while True:
        try:
            sites = SiteManager.get_all_sites()
            now_ts = time.time()
            for site in sites:
                s_id = site["id"]
                s_state = SiteManager.get_site_state(s_id)
                if s_state.get("is_paused", False) or site.get("paused", False) or not site.get("enabled", True):
                    continue

                due_ts = s_state.get("next_run_due", 0)
                if due_ts > 0 and now_ts >= due_ts:
                    interval_sec = max(60, safe_int(site.get("interval_minutes"), 30) * 60)
                    s_state["next_run_due"] = now_ts + interval_sec

                    # Spawn dedicated worker thread for this site (Zero-conflict parallel execution)
                    t = threading.Thread(target=execute_site_cycle, args=(s_id,), daemon=True)
                    t.start()
        except Exception as e:
            bot_log(f"[ERROR] Scheduler master loop error: {e}")

        time.sleep(2)


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
