"""
config.py — Central configuration loader.
All env vars are read once here; every other module imports from this file.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ─── Telegram ──────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN: str = os.environ["TELEGRAM_BOT_TOKEN"]

# ─── Google OAuth ───────────────────────────────────────────────────────────────
GOOGLE_CLIENT_ID: str     = os.environ["GOOGLE_CLIENT_ID"]
GOOGLE_CLIENT_SECRET: str = os.environ["GOOGLE_CLIENT_SECRET"]
GOOGLE_REDIRECT_URI: str  = os.getenv("GOOGLE_REDIRECT_URI", "urn:ietf:wg:oauth:2.0:oob")
GOOGLE_SCOPES: list       = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",
]

# ─── OpenRouteService ───────────────────────────────────────────────────────────
ORS_API_KEY: str = os.getenv("ORS_API_KEY", "")

# ─── User Defaults ───────────────────────────────────────────────────────────────
USER_TIMEZONE: str          = os.getenv("USER_TIMEZONE", "Africa/Lagos")
MORNING_SUMMARY_TIME: str   = os.getenv("MORNING_SUMMARY_TIME", "07:00")
EVENING_RECAP_TIME: str     = os.getenv("EVENING_RECAP_TIME", "21:00")

# ─── Database ────────────────────────────────────────────────────────────────────
DB_PATH: str = os.path.join(os.path.dirname(__file__), "data", "assistant.db")
DB_URL: str  = f"sqlite:///{DB_PATH}"
