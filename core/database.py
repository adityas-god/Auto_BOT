"""
MongoDB Atlas & Local Storage Synchronization Engine for GreyOrange OpsBot.
Provides thread-safe in-memory caching, non-blocking asynchronous cloud sync,
and site/link CRUD operations with credential masking.
"""

import os
import sys
import re
import time
import json
import threading
from urllib.parse import urlparse

from core.config import (
    BASE_DIR,
    ENV_PATH,
    SITES_PATH,
    SITES_RUNTIME_PATH,
    MONGODB_URI,
    MONGO_DB_NAME,
    MONGO_SITES_COLLECTION,
    MONGO_SETTINGS_COLLECTION,
    ACTIVE_LINK_LOCK,
    ACTIVE_LINK_RUNS,
    GEMINI_API_KEY,
    _PYMONGO_AVAILABLE,
)
from core.utils import safe_int, safe_float, _make_json_safe, format_slack_message
from core.logger import bot_log

if _PYMONGO_AVAILABLE:
    # pyrefly: ignore [missing-import]
    from pymongo import MongoClient
    # pyrefly: ignore [missing-import]
    from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError

# ---------------------------------------------------------------------------
# MongoDB connection - module-level singleton (thread-safe)
# ---------------------------------------------------------------------------
_mongo_client = None
_mongo_db = None
_mongo_conn_lock = threading.Lock()   # Ensures only one thread creates the client
_mongo_write_lock = threading.Lock()  # Serialises background write threads


def _get_mongo_db():
    """Return a cached MongoDB database handle. Thread-safe singleton."""
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
        _mongo_db = client[MONGO_DB_NAME]
        print(f"[MONGO] Connected → {MONGO_DB_NAME}", flush=True)
        return _mongo_db


class SiteManager:
    _lock = threading.RLock()
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
            "grafana_theme": os.getenv("GRAFANA_THEME", "dark").strip() or "dark",
            "gemini_api_key": os.getenv("GEMINI_API_KEY", "").strip() or GEMINI_API_KEY
        }

    @classmethod
    def _default_site_from_env(cls):
        return {
            "id": "site_1",
            "name": "Site 1 (Primary)",
            "enabled": True,
            "paused": True,  # Strictly paused by default unless explicitly started
            "bundle_screenshots": os.getenv("BUNDLE_SCREENSHOTS", "false").strip().lower() == "true",
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
                "ai_mode": os.getenv("THRESHOLD_AI_MODE", "false").strip().lower() == "true",
                "ai_prompt": os.getenv("THRESHOLD_AI_PROMPT", "").strip(),
                "metric_type": os.getenv("THRESHOLD_METRIC_TYPE", "spike_jump").strip() or "spike_jump",
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
        return cls._mongo_load_safe()

    @classmethod
    def _mongo_load_safe(cls):
        """
        Load all data from MongoDB. No lock required - caller decides.
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

    @classmethod
    def _mongo_save_all_no_lock(cls):
        """
        Persist the full in-memory cache to MongoDB.
        Uses print() only - never bot_log() - to avoid re-entrant lock deadlock.
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
    _mongo_ready = False   # True once background thread confirms Atlas connection
    _init_started = False  # Prevents spawning multiple background threads
    _last_disk_mtime = 0

    @classmethod
    def _load_local_sites_file(cls):
        """Synchronously load persistent sites_runtime.json or sites.json seed."""
        target_path = SITES_RUNTIME_PATH if (os.path.exists(SITES_RUNTIME_PATH) and os.path.isfile(SITES_RUNTIME_PATH)) else SITES_PATH
        if os.path.exists(target_path) and os.path.isfile(target_path):
            try:
                with open(target_path, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                    if content:
                        loaded = json.loads(content)
                        if isinstance(loaded, dict) and "sites" in loaded:
                            data = _make_json_safe(loaded)
                            # Seed runtime file immediately if it was loaded from sites.json
                            if target_path != SITES_RUNTIME_PATH:
                                try:
                                    with open(SITES_RUNTIME_PATH, "w", encoding="utf-8") as rf:
                                        json.dump(data, rf, indent=2)
                                except Exception:
                                    pass
                            return data
            except Exception as e:
                print(f"[STARTUP][WARN] Could not parse {os.path.basename(target_path)}: {e}", flush=True)
        return None

    @classmethod
    def init(cls):
        """Initialise in-memory cache synchronously from local backup and trigger background Atlas sync."""
        with cls._lock:
            disk_mtime = 0
            target_path = SITES_RUNTIME_PATH if os.path.exists(SITES_RUNTIME_PATH) else SITES_PATH
            if os.path.exists(target_path):
                try:
                    disk_mtime = os.path.getmtime(target_path)
                except Exception:
                    pass

            if cls._data is not None and disk_mtime <= getattr(cls, "_last_disk_mtime", 0):
                return

            local_data = cls._load_local_sites_file()
            if local_data and local_data.get("sites"):
                cls._data = local_data
                cls._last_disk_mtime = disk_mtime
                defaults = cls._default_global_settings()
                gs = cls._data.setdefault("global_settings", defaults)
                for k, v in defaults.items():
                    if not gs.get(k) and v:
                        gs[k] = v
                print(f"[STARTUP] Loaded {len(cls._data['sites'])} site(s) immediately from local storage.", flush=True)
            else:
                cls._data = {
                    "global_settings": cls._default_global_settings(),
                    "sites": []
                }
                default_site = cls._default_site_from_env()
                if default_site.get("grafana_url"):
                    cls._data["sites"].append(default_site)

            cls._sync_auth_state_no_lock()

            now_ts = time.time()
            for site in cls._data.get("sites", []):
                sid = site["id"]
                cls._site_locks.setdefault(sid, threading.RLock())
                # Strictly enforce paused default (must be True unless explicitly False in active session)
                is_p = bool(site.get("paused", True))
                site["paused"] = is_p
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
    def _sync_auth_state_no_lock(cls):
        """
        Synchronizes valid grafana_session cookies from grafana_auth_state.json
        into matching sites and link tabs across in-memory state.
        Ensures Google SSO session tokens captured from Chrome are immediately available.
        """
        try:
            auth_path = os.path.join(BASE_DIR, "grafana_auth_state.json")
            if not os.path.isfile(auth_path):
                return
            with open(auth_path, "r", encoding="utf-8") as f:
                st = json.load(f)
            cookies = st.get("cookies", [])
            tokens_by_host = {}
            for ck in cookies:
                if ck.get("name") == "grafana_session":
                    val = str(ck.get("value", "")).strip()
                    dom = str(ck.get("domain", "")).lower().lstrip(".")
                    if val and len(val) >= 16 and not val.startswith("•"):
                        tokens_by_host[dom] = val

            if not tokens_by_host:
                return

            def _get_host(u):
                try:
                    return (urlparse(str(u or "")).hostname or "").lower()
                except Exception:
                    return ""

            for site in cls._data.get("sites", []):
                s_host = _get_host(site.get("grafana_url"))
                for dom, tok in tokens_by_host.items():
                    if s_host and (dom in s_host or s_host in dom):
                        if not site.get("grafana_token") or len(str(site.get("grafana_token", "")).strip()) < 16:
                            site["grafana_token"] = tok

                for lk in site.get("links", []):
                    l_host = _get_host(lk.get("url")) or s_host
                    for dom, tok in tokens_by_host.items():
                        if l_host and (dom in l_host or l_host in dom):
                            cur_tok = str(lk.get("token") or "").strip()
                            if not cur_tok or len(cur_tok) < 16 or cur_tok.startswith("•"):
                                lk["token"] = tok
        except Exception as e:
            print(f"[AUTH_SYNC] Notice: {e}", flush=True)

    @classmethod
    def _mongo_connect_background(cls):
        """
        Background thread: Connect to Atlas, merge with in-memory sites.
        Performs non-destructive link-by-link merge preserving local tokens,
        destinations, and schedules.
        """
        try:
            _get_mongo_db()
            mongo_loaded = cls._mongo_load_safe()

            with cls._lock:
                now_ts = time.time()
                if mongo_loaded.get("sites"):
                    atlas_ids = {s["id"] for s in mongo_loaded["sites"]}
                    unpushed_local_sites = [s for s in cls._data.get("sites", []) if s["id"] not in atlas_ids]

                    merged_sites = []
                    for ms in mongo_loaded["sites"]:
                        ms_id = ms["id"]
                        local_target = next((s for s in cls._data.get("sites", []) if s["id"] == ms_id), None)
                        if local_target:
                            for rt_key in ("last_run_time", "last_run_ts", "last_status"):
                                if local_target.get(rt_key) and not ms.get(rt_key):
                                    ms[rt_key] = local_target[rt_key]
                            if local_target.get("paused") is not None:
                                ms["paused"] = local_target["paused"]

                            # Cloud Atlas is authoritative for all configured link parameters.
                            local_links = local_target.get("links") or []
                            atlas_links = ms.get("links") or []
                            if not atlas_links and local_links:
                                ms["links"] = local_links
                            elif atlas_links:
                                atlas_l_ids = {l["id"] for l in atlas_links}
                                new_local_lks = [l for l in local_links if l.get("id") not in atlas_l_ids]
                                ms["links"] = atlas_links + new_local_lks

                            if local_target.get("links_initialized"):
                                ms["links_initialized"] = True
                        if "paused" not in ms or ms["paused"] is None:
                            ms["paused"] = True
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

                # Ensure active session tokens from Chrome are synced
                cls._sync_auth_state_no_lock()

                for site in cls._data.get("sites", []):
                    sid = site["id"]
                    cls._site_locks.setdefault(sid, threading.RLock())
                    is_p = bool(site.get("paused", True))
                    site["paused"] = is_p
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
                with open(SITES_RUNTIME_PATH, "w", encoding="utf-8") as rf:
                    json.dump(snapshot, rf, indent=2)
            except Exception:
                pass
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
        2. Fires a background thread to push to Atlas - never blocks the caller.
        """
        # 1. Local backup (write persistent runtime file and seed file)
        try:
            with open(SITES_RUNTIME_PATH, "w", encoding="utf-8") as rf:
                json.dump(cls._data, rf, indent=2)
        except Exception:
            pass
        try:
            with open(SITES_PATH, "w", encoding="utf-8") as f:
                json.dump(cls._data, f, indent=2)
            target_path = SITES_RUNTIME_PATH if os.path.exists(SITES_RUNTIME_PATH) else SITES_PATH
            cls._last_disk_mtime = os.path.getmtime(target_path) if os.path.exists(target_path) else time.time()
        except Exception as exc:
            print(f"[WARN] sites.json write failed: {exc}", flush=True)

        # 2. Deep-copy snapshot (still inside lock, but just json round-trip - fast)
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
                "GEMINI_API_KEY": g.get("gemini_api_key", ""),
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
                cls._site_locks[site_id] = threading.RLock()
            return cls._site_locks[site_id]

    @classmethod
    def get_site_state(cls, site_id):
        cls.init()
        with cls._lock:
            if site_id not in cls._site_states:
                site = next((s for s in cls._data.get("sites", []) if s["id"] == site_id), None)
                is_p = bool(site.get("paused", True)) if site else True
                cls._site_states[site_id] = {
                    "is_running": False,
                    "is_paused": is_p,
                    "last_run_time": site.get("last_run_time") if site else None,
                    "last_status": "Paused" if is_p else "Idle",
                    "next_run_due": 0 if is_p else time.time() + (max(1, safe_int(site.get("interval_minutes", 30))) * 60)
                }
            return cls._site_states[site_id]

    @classmethod
    def set_site_state(cls, site_id, **kwargs):
        cls.init()
        with cls._lock:
            if site_id not in cls._site_states:
                site = next((s for s in cls._data.get("sites", []) if s["id"] == site_id), None)
                is_p = bool(site.get("paused", True)) if site else True
                cls._site_states[site_id] = {
                    "is_running": False,
                    "is_paused": is_p,
                    "last_run_time": site.get("last_run_time") if site else None,
                    "last_status": "Paused" if is_p else "Idle",
                    "next_run_due": 0 if is_p else time.time() + (max(1, safe_int(site.get("interval_minutes", 30))) * 60)
                }
            cls._site_states[site_id].update(kwargs)

    @classmethod
    def record_site_run(cls, site_id, last_run_time, last_run_ts, last_status):
        cls.init()
        ts_val = safe_float(last_run_ts, 0.0)
        with cls._lock:
            for s in cls._data.get("sites", []):
                if s["id"] == site_id:
                    s["last_run_time"] = last_run_time
                    s["last_run_ts"] = ts_val
                    s["last_status"] = last_status
                    break
            st = cls._site_states.get(site_id)
            if st:
                st["last_run_time"] = last_run_time
                st["last_run_ts"] = ts_val
                st["last_status"] = last_status
            cls._save_data_no_lock()

    @classmethod
    def get_global_settings(cls):
        cls.init()
        with cls._lock:
            gs = dict(cls._data.get("global_settings", cls._default_global_settings()))
            if not gs.get("slack_bot_token"):
                gs["slack_bot_token"] = os.getenv("SLACK_BOT_TOKEN", "").strip()
            if not gs.get("gemini_api_key"):
                gs["gemini_api_key"] = os.getenv("GEMINI_API_KEY", "").strip() or GEMINI_API_KEY
            return _make_json_safe(gs)

    @classmethod
    def update_global_settings(cls, updates):
        cls.init()
        with cls._lock:
            g = cls._data.setdefault("global_settings", cls._default_global_settings())
            for k, v in updates.items():
                if k in ("slack_bot_token", "gemini_api_key") and str(v).startswith("••••"):
                    continue   # ignore placeholder from UI
                g[k] = v
            result = _make_json_safe(dict(g))
            cls._save_data_no_lock()
        return result

    @classmethod
    def get_timezone(cls):
        """Read timezone WITHOUT acquiring cls._lock - reads from already-initialised _data."""
        if cls._data is None:
            cls.init()
        gs = cls._data.get("global_settings", {}) if cls._data else {}
        return gs.get("timezone") or "Asia/Kolkata"

    @classmethod
    def create_site(cls, name, clone_from_id=None, grafana_url=""):
        """
        Create a new site, persist it, return a safe (masked) copy for the API.
        bot_log is called AFTER the lock is released to prevent deadlock.
        """
        cls.init()
        log_msg = ""
        safe_ret = None
        with cls._lock:
            new_id = f"site_{int(time.time() * 1000)}"
            while any(s["id"] == new_id for s in cls._data.get("sites", [])):
                new_id = f"site_{int(time.time() * 1000) + 1}"

            base = None
            if clone_from_id:
                for s in cls._data.get("sites", []):
                    if s["id"] == clone_from_id:
                        base = json.loads(json.dumps(s))
                        break
            if not base:
                base = cls._default_site_from_env()

            new_site = _make_json_safe(dict(base))
            new_site["id"] = new_id
            new_site["name"] = name.strip() or f"Site {len(cls._data['sites']) + 1}"
            new_site["paused"] = True
            new_site["last_run_time"] = None
            new_site["last_run_ts"] = 0
            new_site["last_status"] = "Paused"
            if grafana_url:
                new_site["grafana_url"] = grafana_url.strip()

            if clone_from_id:
                new_site["slack_channel_id"] = ""
                new_site["slack_thread_ts"] = ""
                if "threshold" in new_site and isinstance(new_site["threshold"], dict):
                    new_site["threshold"]["breach_users"] = ""
                new_site["shifts"] = {
                    "morning_hours": "06:00-14:00",
                    "morning_users": "",
                    "afternoon_hours": "14:00-22:00",
                    "afternoon_users": "",
                    "night_hours": "22:00-06:00",
                    "night_users": "",
                    "tag_channel": True,
                    "send_dm": True
                }

            if "links" in new_site and isinstance(new_site["links"], list):
                for lk in new_site["links"]:
                    lk["last_run_time"] = None
                    lk["last_run_ts"] = 0
                    lk["last_reading"] = None
                    lk["last_status"] = "Idle"
                    if clone_from_id:
                        lk["slack_channel_id"] = ""
                        lk["slack_thread_ts"] = ""
                        if "threshold" in lk and isinstance(lk["threshold"], dict):
                            lk["threshold"]["breach_users"] = ""
                        if "shifts" in lk and isinstance(lk["shifts"], dict):
                            lk["shifts"]["morning_users"] = ""
                            lk["shifts"]["afternoon_users"] = ""
                            lk["shifts"]["night_users"] = ""

            cls._data["sites"].append(new_site)
            cls._site_locks[new_id] = threading.RLock()
            cls._site_states[new_id] = {
                "is_running": False,
                "is_paused": True,
                "last_run_time": None,
                "last_status": "Paused",
                "next_run_due": 0,
            }
            cls._save_data_no_lock()

            safe_ret = dict(new_site)
            safe_ret["grafana_password_set"] = bool(safe_ret.get("grafana_password"))
            safe_ret["grafana_token_set"] = bool(safe_ret.get("grafana_token"))
            if safe_ret.get("grafana_password"): safe_ret["grafana_password"] = "•" * 16
            if safe_ret.get("grafana_token"): safe_ret["grafana_token"] = "•" * 16
            safe_ret["state"] = dict(cls._site_states[new_id])

            log_msg = f"[SUCCESS] Created site '{new_site['name']}' ({new_id})"

        bot_log(log_msg)
        return safe_ret

    @classmethod
    def update_site(cls, site_id, updates):
        """
        Apply updates dict to a site and persist. Returns (True, safe_copy) or (False, error_str).
        """
        cls.init()
        log_msg = ""
        safe_ret = None
        with cls._lock:
            target = next((s for s in cls._data.get("sites", []) if s["id"] == site_id), None)
            if not target:
                return False, f"Site '{site_id}' not found."

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

            for pw_field in ("grafana_password", "grafana_token"):
                if pw_field in updates:
                    v = str(updates[pw_field]).strip()
                    if not v.startswith("••••"):
                        target[pw_field] = v
            if "interval_minutes" in updates:
                target["interval_minutes"] = max(1, safe_int(updates["interval_minutes"], 30))
            if "paused" in updates:
                is_p = bool(updates["paused"])
                target["paused"] = is_p
                st = cls._site_states.get(site_id)
                if st:
                    st["is_paused"] = is_p
                    st["last_status"] = "Paused" if is_p else "Idle"
            if "bundle_screenshots" in updates:
                target["bundle_screenshots"] = bool(updates["bundle_screenshots"])

            thresh = target.setdefault("threshold", {})
            th_map = {
                "threshold_enabled": ("enabled", bool),
                "threshold_ai_mode": ("ai_mode", bool),
                "threshold_ai_prompt": ("ai_prompt", str),
                "threshold_metric_type": ("metric_type", str),
                "threshold_operator": ("operator", str),
                "threshold_value": ("value", str),
                "threshold_keywords": ("keywords", str),
                "threshold_colors": ("colors", str),
                "threshold_breach_users": ("breach_users", str),
                "threshold_only_alert_on_breach": ("only_alert_on_breach", bool),
            }
            for upd_key, (tgt_key, cast) in th_map.items():
                if upd_key in updates:
                    v = updates[upd_key]
                    thresh[tgt_key] = cast(v) if cast is bool else cast(v).strip()

            shifts = target.setdefault("shifts", {})
            sh_map = {
                "shift_morning_hours": ("morning_hours", str),
                "shift_morning_users": ("morning_users", str),
                "shift_afternoon_hours": ("afternoon_hours", str),
                "shift_afternoon_users": ("afternoon_users", str),
                "shift_night_hours": ("night_hours", str),
                "shift_night_users": ("night_users", str),
                "shift_tag_channel": ("tag_channel", bool),
                "shift_send_dm": ("send_dm", bool),
            }
            for upd_key, (tgt_key, cast) in sh_map.items():
                if upd_key in updates:
                    v = updates[upd_key]
                    shifts[tgt_key] = cast(v) if cast is bool else cast(v).strip()

            if target.get("bundle_screenshots"):
                # When bundle mode is ON, propagate shared configurations to all links under this site
                for lk in target.get("links", []):
                    if "interval_minutes" in updates:
                        lk["interval_minutes"] = target["interval_minutes"]
                    if "slack_message" in updates and str(updates["slack_message"]).strip():
                        lk["slack_message"] = target["slack_message"]
                    if "slack_channel_id" in updates and str(updates["slack_channel_id"]).strip():
                        lk["slack_channel_id"] = target["slack_channel_id"]
                    if "slack_thread_ts" in updates:
                        lk["slack_thread_ts"] = target["slack_thread_ts"]
                    if "paused" in updates:
                        lk["enabled"] = not target["paused"]
                        lk["last_status"] = "Paused" if target["paused"] else "Idle"
                    if "threshold" in target and isinstance(target["threshold"], dict):
                        lk["threshold"] = json.loads(json.dumps(target["threshold"]))
                    if "shifts" in target and isinstance(target["shifts"], dict):
                        lk["shifts"] = json.loads(json.dumps(target["shifts"]))

            cls._save_data_no_lock()

            safe_ret = _make_json_safe(dict(target))
            safe_ret["grafana_password_set"] = bool(target.get("grafana_password"))
            safe_ret["grafana_token_set"] = bool(target.get("grafana_token"))
            if safe_ret.get("grafana_password"): safe_ret["grafana_password"] = "•" * 16
            if safe_ret.get("grafana_token"): safe_ret["grafana_token"] = "•" * 16
            safe_ret["state"] = _make_json_safe(dict(cls._site_states.get(site_id, {})))
            log_msg = f"[SUCCESS] Updated site '{target['name']}' ({site_id})"

        bot_log(log_msg, site_id=site_id)
        return True, safe_ret

    @classmethod
    def get_site_links(cls, site_id, raw=False):
        cls.init()
        with cls._lock:
            for s in cls._data.get("sites", []):
                if s["id"] == site_id:
                    links = s.get("links")
                    if links is None and not s.get("links_initialized") and s.get("grafana_url"):
                        primary_link = {
                            "id": "link_primary",
                            "title": s.get("name", "Primary Dashboard"),
                            "url": s.get("grafana_url", ""),
                            "username": s.get("grafana_username", ""),
                            "password": s.get("grafana_password", ""),
                            "token": s.get("grafana_token", ""),
                            "enabled": True,
                            "slack_channel_id": s.get("slack_channel_id", ""),
                            "slack_thread_ts": s.get("slack_thread_ts", ""),
                            "slack_message": s.get("slack_message", ""),
                            "send_channel": True,
                            "send_dm": s.get("shifts", {}).get("send_dm", True),
                            "only_on_breach": s.get("threshold", {}).get("only_alert_on_breach", False),
                            "threshold": dict(s.get("threshold", {})),
                            "interval_minutes": safe_int(s.get("interval_minutes"), 30),
                            "time_range": "url_default",
                            "last_reading": None,
                            "last_run_time": s.get("last_run_time"),
                            "last_run_ts": s.get("last_run_ts", 0),
                            "last_status": s.get("last_status", "Idle")
                        }
                        s["links"] = [primary_link]
                        s["links_initialized"] = True
                        links = s["links"]
                        cls._save_data_no_lock()
                    elif links is None:
                        s["links"] = []
                        s["links_initialized"] = True
                        links = s["links"]
                    else:
                        s["links_initialized"] = True

                    if raw:
                        return [_make_json_safe(dict(lk)) for lk in links]

                    safe_links = []
                    for lk in links:
                        c = _make_json_safe(dict(lk))
                        c["password_set"] = bool(lk.get("password"))
                        if c.get("password"):
                            c["password"] = "•" * 16
                        c["token_set"] = bool(lk.get("token"))
                        c["token"] = str(lk.get("token") or "")
                        safe_links.append(c)
                    return safe_links
            return []

    @classmethod
    def add_site_link(cls, site_id, link_data):
        cls.init()
        link_id = f"link_{int(time.time() * 1000)}"
        log_msg = ""
        ret = None
        with cls._lock:
            for s in cls._data.get("sites", []):
                if s["id"] == site_id:
                    s["links_initialized"] = True
                    links = s.setdefault("links", [])

                    raw_user = str(link_data.get("username") or "").strip()
                    if raw_user.startswith("http://") or raw_user.startswith("https://"):
                        raw_user = ""
                    raw_pass = str(link_data.get("password") or "").strip()
                    if raw_pass.startswith("•"):
                        raw_pass = ""
                    raw_token = str(link_data.get("token") or link_data.get("session_cookie") or "").strip()
                    if raw_token.startswith("•") or (raw_token.isdigit() and len(raw_token) <= 12):
                        raw_token = ""
                    if not raw_token:
                        try:
                            l_host = (urlparse(str(link_data.get("url") or "")).hostname or "").lower()
                            s_host = (urlparse(str(s.get("grafana_url") or "")).hostname or "").lower()
                            if l_host and s_host and l_host == s_host:
                                raw_token = str(s.get("grafana_token") or "").strip()
                        except Exception:
                            pass
                    elif not s.get("grafana_token") and len(raw_token) >= 16:
                        try:
                            l_host = (urlparse(str(link_data.get("url") or "")).hostname or "").lower()
                            s_host = (urlparse(str(s.get("grafana_url") or "")).hostname or "").lower()
                            if l_host and s_host and l_host == s_host:
                                s["grafana_token"] = raw_token
                        except Exception:
                            pass

                    sibling_ref = next((l for l in s.get("links", []) if l.get("url")), None) or (s.get("links")[0] if s.get("links") else None)
                    is_b = bool(s.get("bundle_screenshots"))
                    b_interval = ((sibling_ref.get("interval_minutes") if sibling_ref else None) or s.get("interval_minutes") or 30) if is_b else 0
                    b_msg = ((sibling_ref.get("slack_message") if sibling_ref else None) or s.get("slack_message")) if is_b else None
                    b_chan = ((sibling_ref.get("slack_channel_id") if sibling_ref else None) or s.get("slack_channel_id", "")) if is_b else None
                    b_thread = ((sibling_ref.get("slack_thread_ts") if sibling_ref else None) or s.get("slack_thread_ts", "")) if is_b else None
                    b_enabled = (not s.get("paused", False)) if is_b else bool(link_data.get("enabled", True))
                    sib_sh = (sibling_ref.get("shifts") or {}) if (is_b and sibling_ref) else (s.get("shifts") or {})
                    sib_th = (sibling_ref.get("threshold") or {}) if (is_b and sibling_ref) else (s.get("threshold") or {})
                    b_send_chan = sibling_ref.get("send_channel", True) if (is_b and sibling_ref) else True
                    b_send_dm = sibling_ref.get("send_dm", True) if (is_b and sibling_ref) else True
                    b_breach_only = sibling_ref.get("only_on_breach", False) if (is_b and sibling_ref) else False

                    new_link = {
                        "id": link_id,
                        "title": str(link_data.get("title") or "Monitored Link").strip(),
                        "url": str(link_data.get("url") or "").strip(),
                        "username": raw_user,
                        "password": raw_pass,
                        "token": raw_token,
                        "interval_minutes": max(0, safe_int(link_data.get("interval_minutes") or b_interval, 0)),
                        "time_range": str(link_data.get("time_range") or (sibling_ref.get("time_range") if is_b and sibling_ref else "url_default")).strip(),
                        "enabled": b_enabled,
                        "slack_channel_id": cls.parse_channel_id(link_data.get("slack_channel_id") or b_chan or s.get("slack_channel_id", "")),
                        "slack_thread_ts": cls.parse_thread_ts(link_data.get("slack_thread_ts") or b_thread or s.get("slack_thread_ts", "")),
                        "slack_message": str(link_data.get("slack_message") or b_msg or s.get("slack_message") or "*Grafana Snapshot Alert* - {datetime}").strip(),
                        "send_channel": bool(link_data.get("send_channel", b_send_chan)),
                        "send_dm": bool(link_data.get("send_dm", b_send_dm)),
                        "only_on_breach": bool(link_data.get("only_on_breach", b_breach_only)),
                        "threshold": {
                            "enabled": bool(link_data.get("threshold_enabled", sib_th.get("enabled", True))),
                            "ai_mode": bool(link_data.get("threshold_ai_mode", sib_th.get("ai_mode", False))),
                            "ai_prompt": str(link_data.get("threshold_ai_prompt") or sib_th.get("ai_prompt", "")).strip(),
                            "metric_type": str(link_data.get("threshold_metric_type") or sib_th.get("metric_type", "spike_jump")).strip(),
                            "operator": str(link_data.get("threshold_operator") or sib_th.get("operator", ">")).strip(),
                            "value": str(link_data.get("threshold_value") or sib_th.get("value", "20")).strip(),
                            "keywords": str(link_data.get("threshold_keywords") or sib_th.get("keywords", "")).strip(),
                            "colors": str(link_data.get("threshold_colors") or sib_th.get("colors", "red, orange, yellow")).strip(),
                            "breach_users": str(link_data.get("threshold_breach_users") or sib_th.get("breach_users", "")).strip(),
                            "only_alert_on_breach": bool(link_data.get("only_on_breach", sib_th.get("only_alert_on_breach", b_breach_only)))
                        },
                        "shifts": {
                            "morning_hours": str((link_data.get("shifts") or {}).get("morning_hours") or sib_sh.get("morning_hours") or "06:00-14:00").strip(),
                            "morning_users": str((link_data.get("shifts") or {}).get("morning_users") or sib_sh.get("morning_users") or "").strip(),
                            "afternoon_hours": str((link_data.get("shifts") or {}).get("afternoon_hours") or sib_sh.get("afternoon_hours") or "14:00-22:00").strip(),
                            "afternoon_users": str((link_data.get("shifts") or {}).get("afternoon_users") or sib_sh.get("afternoon_users") or "").strip(),
                            "night_hours": str((link_data.get("shifts") or {}).get("night_hours") or sib_sh.get("night_hours") or "22:00-06:00").strip(),
                            "night_users": str((link_data.get("shifts") or {}).get("night_users") or sib_sh.get("night_users") or "").strip(),
                            "tag_channel": bool((link_data.get("shifts") or {}).get("tag_channel", sib_sh.get("tag_channel", True))),
                            "send_dm": bool((link_data.get("shifts") or {}).get("send_dm", sib_sh.get("send_dm", True)))
                        },
                        "last_reading": None,
                        "last_run_time": None,
                        "last_run_ts": 0,
                        "last_status": "Idle"
                    }
                    links.append(new_link)
                    cls._save_data_no_lock()
                    log_msg = f"[{s['name']}] Added new monitored link '{new_link['title']}' ({link_id})"
                    ret = _make_json_safe(dict(new_link))
                    if ret.get("password"): ret["password"] = "•" * 16
                    break
        if ret is not None:
            if log_msg:
                bot_log(log_msg, site_id=site_id)
            return True, ret
        return False, "Site not found"

    @classmethod
    def update_site_link(cls, site_id, link_id, updates, log=True):
        cls.init()
        log_msg = ""
        ret = None
        found_site = False
        with cls._lock:
            for s in cls._data.get("sites", []):
                if s["id"] == site_id:
                    found_site = True
                    s["links_initialized"] = True
                    links = s.setdefault("links", [])
                    target = next((lk for lk in links if lk["id"] == link_id), None)
                    if not target:
                        break

                    if "title" in updates: target["title"] = str(updates["title"]).strip()
                    if "url" in updates: target["url"] = str(updates["url"]).strip()
                    if "username" in updates:
                        u_val = str(updates["username"]).strip()
                        if not (u_val.startswith("http://") or u_val.startswith("https://")):
                            target["username"] = u_val
                        elif not target.get("username"):
                            target["username"] = ""

                    if updates.get("clear_password"):
                        target["password"] = ""
                    elif "password" in updates:
                        p_val = str(updates["password"]).strip()
                        if p_val and not p_val.startswith("•"):
                            target["password"] = p_val

                    if updates.get("clear_token") or updates.get("token") == "":
                        target["token"] = ""
                    elif "token" in updates:
                        t_val = str(updates["token"]).strip()
                        if t_val and not t_val.startswith("•") and not (t_val.isdigit() and len(t_val) <= 12):
                            target["token"] = t_val
                            try:
                                t_host = (urlparse(target.get("url") or "").hostname or "").lower()
                                s_host = (urlparse(s.get("grafana_url") or "").hostname or "").lower()
                                if t_host and s_host and t_host == s_host and not s.get("grafana_token") and len(t_val) >= 16:
                                    s["grafana_token"] = t_val
                            except Exception:
                                pass

                    if "interval_minutes" in updates: target["interval_minutes"] = max(0, safe_int(updates["interval_minutes"], 0))
                    if "time_range" in updates: target["time_range"] = str(updates["time_range"]).strip()
                    if "enabled" in updates:
                        target["enabled"] = bool(updates["enabled"])
                        target["last_status"] = "Active" if target["enabled"] else "Paused"
                        if target["enabled"]:
                            s["paused"] = False
                            st = cls._site_states.get(site_id)
                            if st:
                                st["is_paused"] = False
                                st["last_status"] = "Active"
                    if "slack_channel_id" in updates: target["slack_channel_id"] = cls.parse_channel_id(updates["slack_channel_id"])
                    if "slack_thread_ts" in updates: target["slack_thread_ts"] = cls.parse_thread_ts(updates["slack_thread_ts"])
                    if "slack_message" in updates: target["slack_message"] = str(updates["slack_message"]).strip()
                    if "send_channel" in updates: target["send_channel"] = bool(updates["send_channel"])
                    if "send_dm" in updates: target["send_dm"] = bool(updates["send_dm"])
                    if "only_on_breach" in updates: target["only_on_breach"] = bool(updates["only_on_breach"])
                    if "last_reading" in updates: target["last_reading"] = updates["last_reading"]
                    if "last_run_time" in updates: target["last_run_time"] = updates["last_run_time"]
                    if "last_run_ts" in updates: target["last_run_ts"] = float(updates["last_run_ts"])
                    if "last_status" in updates: target["last_status"] = updates["last_status"]

                    th = target.setdefault("threshold", {})
                    if "threshold_enabled" in updates: th["enabled"] = bool(updates["threshold_enabled"])
                    if "threshold_ai_mode" in updates: th["ai_mode"] = bool(updates["threshold_ai_mode"])
                    if "threshold_ai_prompt" in updates: th["ai_prompt"] = str(updates["threshold_ai_prompt"]).strip()
                    if "threshold_metric_type" in updates: th["metric_type"] = str(updates["threshold_metric_type"]).strip()
                    if "threshold_operator" in updates: th["operator"] = str(updates["threshold_operator"]).strip()
                    if "threshold_value" in updates: th["value"] = str(updates["threshold_value"]).strip()
                    if "threshold_keywords" in updates: th["keywords"] = str(updates["threshold_keywords"]).strip()
                    if "threshold_colors" in updates: th["colors"] = str(updates["threshold_colors"]).strip()
                    if "threshold_breach_users" in updates: th["breach_users"] = str(updates["threshold_breach_users"]).strip()
                    if "only_on_breach" in updates: th["only_alert_on_breach"] = bool(updates["only_on_breach"])

                    if "shifts" in updates and isinstance(updates["shifts"], dict):
                        sh = target.setdefault("shifts", {})
                        for k in ("morning_hours", "morning_users", "afternoon_hours", "afternoon_users", "night_hours", "night_users"):
                            if k in updates["shifts"]:
                                sh[k] = str(updates["shifts"][k]).strip()
                        if "tag_channel" in updates["shifts"]:
                            sh["tag_channel"] = bool(updates["shifts"]["tag_channel"])
                        if "send_dm" in updates["shifts"]:
                            sh["send_dm"] = bool(updates["shifts"]["send_dm"])

                    if s.get("bundle_screenshots"):
                        # If bundle mode is ON, changes made on one tab sync to ALL sibling tabs and the site!
                        shared_link_keys = (
                            "interval_minutes", "slack_message", "slack_channel_id",
                            "slack_thread_ts", "send_channel", "send_dm", "only_on_breach",
                            "time_range"
                        )
                        for other_lk in links:
                            if other_lk["id"] == link_id:
                                continue
                            for k in shared_link_keys:
                                if k in updates:
                                    other_lk[k] = target[k]
                            if any(k.startswith("threshold") for k in updates) or "only_on_breach" in updates:
                                other_lk["threshold"] = json.loads(json.dumps(target.get("threshold", {})))
                            if "shifts" in updates:
                                other_lk["shifts"] = json.loads(json.dumps(target.get("shifts", {})))
                            if "enabled" in updates:
                                other_lk["enabled"] = target["enabled"]
                                other_lk["last_status"] = "Active" if target["enabled"] else "Paused"

                        # Keep site-level fallbacks in sync
                        if "interval_minutes" in updates:
                            s["interval_minutes"] = target["interval_minutes"]
                        if "slack_message" in updates:
                            s["slack_message"] = target["slack_message"]
                        if "slack_channel_id" in updates:
                            s["slack_channel_id"] = target["slack_channel_id"]
                        if "slack_thread_ts" in updates:
                            s["slack_thread_ts"] = target["slack_thread_ts"]
                        if "threshold" in target:
                            s["threshold"] = json.loads(json.dumps(target["threshold"]))
                        if "shifts" in target:
                            s["shifts"] = json.loads(json.dumps(target["shifts"]))
                        if "enabled" in updates:
                            s["paused"] = not target["enabled"]
                            st = cls._site_states.get(site_id)
                            if st:
                                st["is_paused"] = s["paused"]
                                st["last_status"] = "Paused" if s["paused"] else "Active"

                    cls._save_data_no_lock()
                    if log:
                        log_msg = f"[{s['name']}] Updated monitored link '{target['title']}' ({link_id})"
                    ret = _make_json_safe(dict(target))
                    if ret.get("password"): ret["password"] = "•" * 16
                    break
        if ret is not None:
            if log_msg:
                bot_log(log_msg, site_id=site_id)
            return True, ret
        if not found_site:
            return False, "Site not found"
        return False, "Link not found"

    @classmethod
    def delete_site_link(cls, site_id, link_id):
        cls.init()
        log_msg = ""
        found = False
        found_site = False
        with cls._lock:
            for s in cls._data.get("sites", []):
                if s["id"] == site_id:
                    found_site = True
                    s["links_initialized"] = True
                    links = s.setdefault("links", [])
                    before_len = len(links)
                    s["links"] = [lk for lk in links if lk["id"] != link_id]
                    if len(s["links"]) < before_len:
                        cls._save_data_no_lock()
                        log_msg = f"[{s['name']}] Removed monitored link '{link_id}'"
                        found = True
                    break
        if found:
            if log_msg:
                bot_log(log_msg, site_id=site_id)
            return True, "Link deleted successfully."
        if not found_site:
            return False, "Site not found"
        return False, "Link not found"

    @classmethod
    def toggle_site_link(cls, site_id, link_id):
        cls.init()
        log_msg = ""
        en_val = None
        found_site = False
        is_bundle_site = False
        target_title = link_id
        site_name = site_id
        with cls._lock:
            for s in cls._data.get("sites", []):
                if s["id"] == site_id:
                    found_site = True
                    is_bundle_site = bool(s.get("bundle_screenshots"))
                    site_name = s.get("name", site_id)
                    links = s.setdefault("links", [])
                    target = next((lk for lk in links if lk["id"] == link_id), None)
                    if target:
                        target["enabled"] = not bool(target.get("enabled", True))
                        en_val = target["enabled"]
                        target_title = target.get("title") or link_id
                        
                        st = cls._site_states.get(site_id)
                        if s.get("bundle_screenshots"):
                            # When bundle is ON, pausing/starting any tab pauses/starts ALL tabs in the bundle
                            s["paused"] = not en_val
                            if st:
                                st["is_paused"] = not en_val
                                st["last_status"] = "Active" if en_val else "Paused"
                            now_ts = time.time()
                            for lk in links:
                                lk["enabled"] = en_val
                                lk["last_status"] = "Capturing..." if en_val else "Paused"
                                if en_val:
                                    lk["last_run_ts"] = now_ts
                            log_msg = f"[{s['name']}] Bundle monitoring {'ENABLED' if en_val else 'PAUSED'} across all {len(links)} tabs."
                        elif en_val:
                            s["paused"] = False
                            if st:
                                st["is_paused"] = False
                                st["last_status"] = "Active"
                            now_ts = time.time()
                            target["last_run_ts"] = now_ts
                            target["last_status"] = "Capturing..."
                            for other_lk in links:
                                if other_lk.get("id") != link_id and other_lk.get("last_run_ts", 0) <= 0:
                                    other_lk["last_run_ts"] = now_ts
                            log_msg = f"[{s['name']}] Link '{target_title}' monitoring is now ENABLED. Site active."
                        else:
                            any_other_enabled = any(lk.get("enabled", True) for lk in links if lk.get("id") != link_id and lk.get("url"))
                            if not any_other_enabled:
                                s["paused"] = True
                                if st:
                                    st["is_paused"] = True
                                    st["last_status"] = "Paused"
                            target["last_status"] = "Paused"
                            log_msg = f"[{s['name']}] Link '{target_title}' monitoring is now DISABLED."

                        cls._save_data_no_lock()
                    break
        if en_val is not None:
            if log_msg:
                bot_log(log_msg, site_id=site_id)
            if en_val:
                if is_bundle_site:
                    bot_log(f"[{site_name}] Dispatching bundled snapshot cycle for all enabled tabs...", site_id=site_id)
                    from scheduler.runner import run_site_bundle_capture_and_alert
                    threading.Thread(target=run_site_bundle_capture_and_alert, args=(site_id, True), daemon=True).start()
                else:
                    run_key = f"{site_id}_{link_id}"
                    with ACTIVE_LINK_LOCK:
                        if run_key not in ACTIVE_LINK_RUNS:
                            ACTIVE_LINK_RUNS.add(run_key)
                            bot_log(f"[{site_name}] Dispatching single instant snapshot for enabled link '{target_title}'...", site_id=site_id)
                            from scheduler.runner import run_link_capture_and_alert
                            threading.Thread(target=run_link_capture_and_alert, args=(site_id, link_id, True, True), daemon=True).start()
                        else:
                            bot_log(f"[{site_name}] Link '{target_title}' capture already in flight, skipping duplicate trigger.", site_id=site_id)
            return True, en_val
        if not found_site:
            return False, "Site not found"
        return False, "Link not found"

    @classmethod
    def delete_site(cls, site_id):
        """Delete a site. Allows deleting the last site (UI handles guard)."""
        cls.init()
        log_msg = ""
        ret_msg = ""
        with cls._lock:
            sites = cls._data.get("sites", [])
            target_idx = None
            site_name = site_id
            for idx, s in enumerate(sites):
                if s["id"] == site_id:
                    target_idx = idx
                    site_name = s.get("name", site_id)
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
        log_msg = ""
        new_val = False
        found = False
        site_name = site_id
        target_link_to_run = None
        with cls._lock:
            for s in cls._data.get("sites", []):
                if s["id"] == site_id:
                    site_name = s.get("name", site_id)
                    curr_p = bool(s.get("paused", True))
                    new_val = not curr_p
                    s["paused"] = new_val
                    st = cls._site_states.get(site_id)
                    if st:
                        st["is_paused"] = new_val
                        st["last_status"] = "Paused" if new_val else "Active"
                        if new_val:
                            st["next_run_due"] = 0
                            for lk in s.get("links", []):
                                lk["enabled"] = False
                                lk["last_status"] = "Paused"
                        else:
                            now_ts = time.time()
                            links = s.get("links", [])
                            for lk in links:
                                if lk.get("url"):
                                    lk["enabled"] = True
                                    lk["last_run_ts"] = now_ts
                                    lk["last_status"] = "Idle"

                            interval_sec = max(60, safe_int(s.get("interval_minutes"), 1) * 60)
                            st["next_run_due"] = now_ts + interval_sec
                    cls._save_data_no_lock()
                    log_msg = f"[INFO] Site '{s['name']}' ({site_id}) monitoring is now {'PAUSED' if new_val else 'STARTED (ACTIVE)'}. Other sites unaffected."
                    found = True
                    break
        if not found:
            return False, "Site not found"
        bot_log(log_msg, site_id=site_id)

        if not new_val:
            from scheduler.scheduler import execute_site_cycle
            bot_log(f"[{site_name}] Start Monitoring activated: initiating capture cycle across active site links...", site_id=site_id)
            threading.Thread(target=execute_site_cycle, args=(site_id, True), daemon=True).start()

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
