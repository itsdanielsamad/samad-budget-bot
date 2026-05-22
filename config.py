"""Environment-driven config — never put secrets in code."""
import json
import os
from dotenv import load_dotenv

load_dotenv()


def _required(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"Missing required env var: {name}")
    return v


TELEGRAM_BOT_TOKEN = _required("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = _required("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
SPREADSHEET_ID = _required("SPREADSHEET_ID")

ALLOWED_TELEGRAM_USER_IDS = {
    int(uid.strip())
    for uid in os.getenv("ALLOWED_TELEGRAM_USER_IDS", "").split(",")
    if uid.strip()
}

PAYER_MAP: dict[int, str] = {}
for pair in os.getenv("PAYER_MAP", "").split(","):
    if ":" in pair:
        uid, name = pair.split(":", 1)
        PAYER_MAP[int(uid.strip())] = name.strip()

TAB_SPENT_BUCKET = os.getenv("TAB_SPENT_BUCKET", "Spent Bucket")
TAB_EXPENSES = os.getenv("TAB_EXPENSES", "Expenses")
TAB_VENDOR_MEMORY = os.getenv("TAB_VENDOR_MEMORY", "Vendor Memory")
TAB_BOT_CONFIG = os.getenv("TAB_BOT_CONFIG", "Bot Config")

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

GOOGLE_SERVICE_ACCOUNT_JSON = _required("GOOGLE_SERVICE_ACCOUNT_JSON")
try:
    GOOGLE_SERVICE_ACCOUNT_INFO = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
except json.JSONDecodeError as e:
    raise RuntimeError(f"GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON: {e}")
