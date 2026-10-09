"""
Global Configuration & Concurrency Management for GreyOrange OpsBot.
Provides path resolutions, environment variable bindings, and thread synchronization primitives.
"""

import os
import sys
import threading
# pyrefly: ignore [missing-import]
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.path.join(BASE_DIR, ".env")
SITES_PATH = os.path.join(BASE_DIR, "sites.json")
SITES_RUNTIME_PATH = os.path.join(BASE_DIR, "sites_runtime.json")

if os.path.isdir(ENV_PATH):
    ENV_PATH = os.path.join(ENV_PATH, "settings.env")

if os.path.exists(ENV_PATH) and os.path.isfile(ENV_PATH):
    load_dotenv(dotenv_path=ENV_PATH, override=True)

# MongoDB configuration - prefer env var, fall back to default URI
MONGODB_URI = os.getenv(
    "MONGODB_URI",
    "mongodb+srv://adityasctr_db_user:ydv9XNaewJtaQjYC@cluster0.ef4bjjj.mongodb.net/operations_db?retryWrites=true&w=majority"
)
MONGO_DB_NAME = "operations_db"
MONGO_SITES_COLLECTION = "opsbot_sites"
MONGO_SETTINGS_COLLECTION = "opsbot_global_settings"

# Global link concurrency control across all threads and sites
ACTIVE_LINK_LOCK = threading.Lock()
ACTIVE_LINK_RUNS = set()

# Global browser capture concurrency limiter (Max 2 simultaneous Playwright browsers to keep RAM < 450MB)
GLOBAL_CAPTURE_SEMAPHORE = threading.Semaphore(2)

# Optional dependency detection
try:
    # pyrefly: ignore [missing-import]
    from pymongo import MongoClient
    # pyrefly: ignore [missing-import]
    from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError
    _PYMONGO_AVAILABLE = True
except ImportError:
    _PYMONGO_AVAILABLE = False

try:
    # pyrefly: ignore [missing-import]
    import pytesseract
    # pyrefly: ignore [missing-import]
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
