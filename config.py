"""
Configuration module for GhostLead Radar.
Loads environment variables and sets monitoring parameters.
"""

import os
import sys
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Required environment variables (only Telegram credentials are required)
REQUIRED_VARS = [
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
]

missing_vars = [var for var in REQUIRED_VARS if not os.getenv(var)]
if missing_vars:
    sys.exit(
        f"Configuration Error: Missing required environment variable(s): {', '.join(missing_vars)}.\n"
        f"Please copy .env.example to .env and configure all required credentials."
    )

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# RSS feed does not need Reddit API credentials (optional / unused)
REDDIT_CLIENT_ID = os.getenv("REDDIT_CLIENT_ID", "").strip()
REDDIT_CLIENT_SECRET = os.getenv("REDDIT_CLIENT_SECRET", "").strip()

# User Agent for Reddit requests
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 GhostLeadRadar/1.0"
)
REDDIT_USER_AGENT = os.getenv("REDDIT_USER_AGENT", DEFAULT_USER_AGENT).strip()

# Polling interval configuration with validation and 15s floor
poll_interval_raw = os.getenv("POLL_INTERVAL", "60").strip()
try:
    parsed_poll_interval = int(poll_interval_raw)
except ValueError:
    sys.exit(
        f"Configuration Error: POLL_INTERVAL must be an integer, got '{poll_interval_raw}'."
    )

# Enforce a minimum floor of 15 seconds to avoid API bans
POLL_INTERVAL = max(15, parsed_poll_interval)

# Subreddits to monitor
SUBREDDITS = [
    "coldemail",
    "sales",
    "leadgeneration",
    "SaaS",
    "Emailmarketing",
]

# Keywords indicating cold email / deliverability pain points.
# Note: "instantly" is generic and can be noisy; user may replace with "instantly.ai"
KEYWORDS = [
    "bounce rate",
    "apollo bounce",
    "apollo data",
    "smartlead",
    "instantly",
    "catch-all",
    "email deliverability",
    "burned domain",
    "smtp handshake",
    "verify email",
    "clean list",
    "dirty list",
]

# number of latest posts to request from the feed (RSS limit param, max 100)
FETCH_LIMIT = 50

# Database path for deduplication
DB_PATH = os.getenv("SEEN_DB_PATH", "seen.db").strip()

# Alert on initial batch on first run if true; silent catch-up if false (default)
ALERT_ON_FIRST_RUN_BACKLOG = os.getenv(
    "ALERT_ON_FIRST_RUN_BACKLOG", "false"
).strip().lower() in ("true", "1", "yes")
