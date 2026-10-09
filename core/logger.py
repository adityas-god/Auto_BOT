"""
Centralized Activity Stream & Console Logging for GreyOrange OpsBot.
Provides real-time timestamped terminal output and a thread-safe ring buffer for the UI Activity Stream.
"""

import threading
from collections import deque
from core.utils import get_current_datetime

LOG_BUFFER = deque(maxlen=250)
_LOGGER_LOCK = threading.Lock()


def bot_log(message, site_id=None):
    """
    Outputs a formatted, timestamped log entry to stdout and appends it to the in-memory LOG_BUFFER.
    """
    try:
        from core.database import SiteManager
        tz = SiteManager.get_timezone()
    except Exception:
        tz = "Asia/Kolkata"

    timestamp = get_current_datetime(tz).strftime("%H:%M:%S")
    entry = f"[{timestamp}] {message}"
    print(entry, flush=True)

    with _LOGGER_LOCK:
        LOG_BUFFER.append({
            "timestamp": timestamp,
            "site_id": site_id or "global",
            "formatted": entry
        })
