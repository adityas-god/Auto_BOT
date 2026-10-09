"""
General Utility Functions for GreyOrange OpsBot.
Includes time handling, URL parameter manipulation, network proxy checks, and Slack message formatting.
"""

import socket
import json
from datetime import datetime
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

from core.config import pytz, ZoneInfo


def apply_grafana_time_range(url, time_from, time_to="now"):
    """
    Seamlessly injects or updates Grafana dashboard/panel query parameters
    for time range ('from' and 'to').
    """
    if not url or not time_from or time_from in ("url_default", "default", "none"):
        return url
    try:
        parsed = urlparse(url)
        params = parse_qs(parsed.query, keep_blank_values=True)
        params["from"] = [time_from]
        params["to"] = [time_to]
        flat_params = []
        for k, v_list in params.items():
            for v in v_list:
                flat_params.append((k, v))
        new_query = urlencode(flat_params)
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, parsed.fragment))
    except Exception:
        return url


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
