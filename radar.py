"""
GhostLead Radar - Real-Time Reddit Social Listening / Intent Radar (RSS Mode)
=============================================================================

Monitors cold email and deliverability pain points across Reddit subreddits
in real time via Reddit's public Atom RSS feeds (no Reddit API keys needed),
deduplicates posts via SQLite, and delivers rich formatted lead alerts
to a Telegram bot.

Setup Steps:
------------
1. Create and activate a Python virtual environment:
   python -m venv venv
   # On Windows (PowerShell):
   .\\venv\\Scripts\\Activate.ps1
   # On Linux/macOS:
   source venv/bin/activate

2. Install project dependencies:
   pip install -r requirements.txt

3. Create and configure your environment file:
   copy .env.example .env     (Windows)
   cp .env.example .env       (Linux/macOS)
   Fill in TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.
   (Reddit API credentials are NOT needed in RSS mode.)

4. Run the radar:
   # Continuous daemon loop:
   python radar.py
   # Single execution (e.g. GitHub Actions cron):
   python radar.py --once
   # Or via environment variable:
   RUN_ONCE=true python radar.py
"""

import argparse
import html
import logging
import os
import re
import signal
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import requests

import config

# Setup structured logging with timestamps and log levels
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("ghostlead-radar")

# Global flag for graceful termination
running = True

# RSS feed configuration and headers
FEED_URL = f"https://www.reddit.com/r/{'+'.join(config.SUBREDDITS)}/new.rss"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/119.0",
    "Accept": "application/atom+xml,application/xml,text/xml",
}
NS = {"atom": "http://www.w3.org/2005/Atom"}


@dataclass
class Post:
    id: str
    title: str
    selftext: str
    author: str  # "u/name" or ""
    permalink: str
    subreddit: str


def handle_shutdown(signum, frame):
    """Handles SIGINT/SIGTERM for immediate graceful shutdown."""
    global running
    sig_name = "SIGINT" if signum == signal.SIGINT else str(signum)
    if hasattr(signal, "SIGTERM") and signum == signal.SIGTERM:
        sig_name = "SIGTERM"
    logger.info("Shutdown signal (%s) received. Cleaning up and exiting...", sig_name)
    running = False


def sleep_interruptible(seconds: float) -> None:
    """
    Sleeps in 1-second chunks while checking the running flag
    so shutdown signals (Ctrl+C) break immediately.
    """
    deadline = time.time() + max(0.0, seconds)
    while running and time.time() < deadline:
        time.sleep(min(1.0, max(0.0, deadline - time.time())))


class SeenStore:
    """
    SQLite-backed persistent deduplication store.
    Ensures restart safety and guarantees no duplicate alerts.
    """

    def __init__(self, db_path: str = "seen.db") -> None:
        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY, ts REAL)"
            )
            conn.commit()
        finally:
            conn.close()

    def has(self, post_id: str) -> bool:
        """Returns True if the post ID has already been recorded."""
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.execute(
                "SELECT 1 FROM seen WHERE id = ? LIMIT 1", (post_id,)
            )
            return cursor.fetchone() is not None
        finally:
            conn.close()

    def add(self, post_id: str) -> None:
        """Records a post ID with the current Unix timestamp."""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT OR IGNORE INTO seen (id, ts) VALUES (?, ?)",
                (post_id, time.time()),
            )
            conn.commit()
        finally:
            conn.close()

    def is_empty(self) -> bool:
        """Returns True if the seen table contains no entries."""
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.execute("SELECT 1 FROM seen LIMIT 1")
            return cursor.fetchone() is None
        finally:
            conn.close()


def escape_markdown_v2(text: str) -> str:
    """
    Escapes characters reserved in Telegram MarkdownV2 text fields:
    _ * [ ] ( ) ~ ` > # + - = | { } . ! \\
    """
    if not text:
        return ""
    return re.sub(r"([_*\[\]()~`>#+\-=|{}.!\\])", r"\\\1", str(text))


def escape_url(url: str) -> str:
    """
    Escapes characters in Telegram MarkdownV2 inline URL targets.
    In inline link targets, only ')' and '\\' must be escaped.
    """
    if not url:
        return ""
    return re.sub(r"([)\\])", r"\\\1", str(url))


def find_matching_keywords(text: str, keywords: List[str]) -> List[str]:
    """
    Performs case-insensitive regex matching with non-word boundaries
    and escaped keywords against the provided text.
    Returns a deduplicated list of matched keywords.
    """
    matched = []
    for kw in keywords:
        pattern = rf"(?<!\w){re.escape(kw)}(?!\w)"
        if re.search(pattern, text, re.IGNORECASE):
            matched.append(kw)
    return list(dict.fromkeys(matched))


def extract_snippet(body: str, matched_keywords: List[str]) -> str:
    """
    Extracts a 2-3 sentence snippet of the pain point:
    - Collapses whitespace
    - Splits into sentences
    - Starts from the first sentence containing a matched keyword
    - Takes up to 3 sentences
    - Caps at ~320 chars with "…"
    - If body is empty, returns fallback text noting keyword was found in title.
    """
    cleaned = (body or "").strip()
    if not cleaned or cleaned in ("[removed]", "[deleted]"):
        return "Keyword found in post title (no post body text)."

    # Collapse whitespace into single spaces
    collapsed = " ".join(cleaned.split())

    # Split into sentences based on terminating punctuation (.!?) followed by space
    sentences = [
        s.strip()
        for s in re.split(r"(?<=[.!?])\s+", collapsed)
        if s.strip()
    ]

    if not sentences:
        return "Keyword found in post title (no post body text)."

    # Locate first sentence containing any of the matched keywords
    start_idx = 0
    for idx, sentence in enumerate(sentences):
        if any(
            re.search(rf"(?<!\w){re.escape(kw)}(?!\w)", sentence, re.IGNORECASE)
            for kw in matched_keywords
        ):
            start_idx = idx
            break

    # Select up to 3 sentences starting from the matched sentence
    selected_sentences = sentences[start_idx : start_idx + 3]
    snippet = " ".join(selected_sentences).strip()

    # Cap at ~320 characters
    if len(snippet) > 320:
        snippet = snippet[:319].rstrip() + "…"

    return snippet


def clean_html(raw: str) -> str:
    """
    Cleans raw HTML content from Reddit RSS:
    1. Strips HTML tags using re.sub(r'<[^>]+>', ' ', raw)
    2. Decodes HTML entities via html.unescape
    3. Collapses whitespace
    4. Removes Reddit's trailing footer with re.sub(r"submitted by\s+/?u/\S+.*$", "", text, flags=re.IGNORECASE)
    """
    if not raw:
        return ""
    text = re.sub(r"<[^>]+>", " ", raw)
    text = html.unescape(text)
    text = " ".join(text.split())
    text = re.sub(r"submitted by\s+/?u/\S+.*$", "", text, flags=re.IGNORECASE)
    return text.strip()


def _get_text(element: Optional[ET.Element], path: str, namespaces: Dict[str, str] = NS) -> str:
    """Safely extracts text from a child element, returning '' if missing or empty."""
    if element is None:
        return ""
    child = element.find(path, namespaces)
    if child is not None and child.text:
        return child.text
    return ""


def parse_feed(xml_text: str) -> List[Post]:
    """
    Parses Reddit Atom RSS XML feed into a list of Post objects.
    """
    root = ET.fromstring(xml_text)
    posts: List[Post] = []

    for entry in root.findall("atom:entry", NS):
        id_raw = _get_text(entry, "atom:id").strip()
        post_id = id_raw.split(":")[-1].strip() if id_raw else ""
        if not post_id:
            continue

        title = _get_text(entry, "atom:title").strip()
        content_raw = _get_text(entry, "atom:content")
        selftext = clean_html(content_raw)

        author_text = _get_text(entry, "atom:author/atom:name").strip()
        if author_text.startswith("/u/"):
            author_name = author_text[3:]
        elif author_text.startswith("u/"):
            author_name = author_text[2:]
        else:
            author_name = author_text

        if author_name and author_name not in ("[deleted]", "[removed]"):
            author = f"u/{author_name}"
        else:
            author = ""

        link_elem = entry.find("atom:link", NS)
        permalink = link_elem.attrib.get("href", "").strip() if link_elem is not None else ""

        category_elem = entry.find("atom:category", NS)
        subreddit = ""
        if category_elem is not None:
            subreddit = category_elem.attrib.get("term", "").strip()
        if not subreddit and permalink:
            match = re.search(r"/r/([^/]+)/", permalink)
            if match:
                subreddit = match.group(1).strip()
        if not subreddit:
            subreddit = "unknown"

        posts.append(
            Post(
                id=post_id,
                title=title,
                selftext=selftext,
                author=author,
                permalink=permalink,
                subreddit=subreddit,
            )
        )

    return posts


def fetch_new_posts() -> List[Post]:
    """
    Fetches the latest submissions from Reddit's public Atom RSS feed.
    """
    r = requests.get(
        FEED_URL,
        headers=HEADERS,
        params={"limit": config.FETCH_LIMIT},
        timeout=20,
    )
    r.raise_for_status()
    return parse_feed(r.text)


def format_alert(post: Post, matched_keywords: List[str]) -> str:
    """
    Formats the Telegram alert using MarkdownV2:
    🚨 *High-Intent Lead Detected*

    *Subreddit:* r/<name>
    *Author:* u/<author> (or [deleted])
    *Matched:* <keywords>

    *Title:* <title>

    _<2-3 sentence snippet of the pain point>_

    👉 [Open on Reddit & reply](<permalink>)
    """
    subreddit = getattr(post, "subreddit", "")
    author_val = getattr(post, "author", "") or "[deleted]"
    title = getattr(post, "title", "")
    selftext = getattr(post, "selftext", "")
    link = getattr(post, "permalink", "")

    escaped_sub = escape_markdown_v2(subreddit)
    escaped_author = escape_markdown_v2(author_val)
    escaped_matched = escape_markdown_v2(", ".join(matched_keywords))
    escaped_title = escape_markdown_v2(title or "")

    snippet = extract_snippet(selftext or "", matched_keywords)
    escaped_snippet = escape_markdown_v2(snippet)

    escaped_link_target = escape_url(link)

    # Note: hyphen in High\-Intent must be escaped for MarkdownV2
    message = (
        "🚨 *High\\-Intent Lead Detected*\n\n"
        f"*Subreddit:* r/{escaped_sub}\n"
        f"*Author:* {escaped_author}\n"
        f"*Matched:* {escaped_matched}\n\n"
        f"*Title:* {escaped_title}\n\n"
        f"_{escaped_snippet}_\n\n"
        f"👉 [Open on Reddit & reply]({escaped_link_target})"
    )
    return message


def send_telegram(message_text: str) -> bool:
    """
    Dispatches a MarkdownV2 alert to Telegram:
    - 5 retries.
    - HTTP 429: sleeps retry_after + 1 seconds.
    - Other 4xx: logs error and returns False immediately (no pointless retry).
    - 5xx / requests.RequestException: exponential backoff capped at 60s.
    """
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": message_text,
        "parse_mode": "MarkdownV2",
    }

    max_retries = 5
    backoff = 2.0

    for attempt in range(1, max_retries + 1):
        if not running:
            logger.info("Shutdown in progress. Aborting Telegram alert delivery.")
            return False

        try:
            response = requests.post(url, json=payload, timeout=15)

            if response.status_code == 200:
                logger.info("Telegram alert sent successfully.")
                return True

            if response.status_code == 429:
                retry_after = 5
                try:
                    data = response.json()
                    parameters = data.get("parameters", {})
                    if "retry_after" in parameters:
                        retry_after = int(parameters["retry_after"])
                except Exception:
                    if "Retry-After" in response.headers:
                        try:
                            retry_after = int(response.headers["Retry-After"])
                        except (ValueError, TypeError):
                            pass

                sleep_duration = retry_after + 1
                logger.warning(
                    "Telegram rate limited (HTTP 429). Retrying in %d seconds (attempt %d/%d)...",
                    sleep_duration,
                    attempt,
                    max_retries,
                )
                sleep_interruptible(sleep_duration)
                continue

            if 400 <= response.status_code < 500:
                logger.error(
                    "Telegram client error HTTP %d (non-retryable): %s",
                    response.status_code,
                    response.text,
                )
                return False

            # Server error 5xx
            logger.warning(
                "Telegram server error HTTP %d on attempt %d/%d: %s. Backing off for %.1fs...",
                response.status_code,
                attempt,
                max_retries,
                response.text,
                backoff,
            )
            sleep_interruptible(backoff)
            backoff = min(60.0, backoff * 2)

        except requests.RequestException as exc:
            logger.warning(
                "Network error connecting to Telegram on attempt %d/%d: %s. Backing off for %.1fs...",
                attempt,
                max_retries,
                exc,
                backoff,
            )
            sleep_interruptible(backoff)
            backoff = min(60.0, backoff * 2)

    logger.error("Failed to send Telegram alert after %d attempts.", max_retries)
    return False


def process(post: Post, seen_store: Optional[SeenStore] = None) -> None:
    """
    Processes an individual Post:
    - Checks deduplication store
    - Matches configured keywords against title and selftext
    - Dispatches Telegram alert if matched
    - Marks as seen only after successful Telegram send
    """
    store = seen_store or SeenStore(config.DB_PATH)

    if store.has(post.id):
        return

    # Check title + selftext against configured pain point keywords
    combined_text = f"{post.title}\n{post.selftext}"
    matched = find_matching_keywords(combined_text, config.KEYWORDS)

    if not matched:
        # Mark posts with no match as seen immediately
        store.add(post.id)
        return

    logger.info(
        "Lead matched in r/%s: %s (keywords: %s)",
        post.subreddit,
        post.id,
        ", ".join(matched),
    )

    alert_message = format_alert(post, matched)
    success = send_telegram(alert_message)

    if success:
        # IMPORTANT: mark seen ONLY after Telegram send succeeds
        store.add(post.id)
    else:
        logger.warning(
            "Delivery failed for post %s; will retry next polling cycle.",
            post.id,
        )

    # Sleep 1 second between Telegram sends to prevent flooding
    sleep_interruptible(1.0)


def parse_arguments(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """Parses command-line arguments for GhostLead Radar."""
    parser = argparse.ArgumentParser(
        description="GhostLead Radar - Real-Time Reddit Social Listening / Intent Radar"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single polling cycle and exit cleanly without entering the continuous loop",
    )
    return parser.parse_args(argv)


def is_single_run_mode(args: Optional[argparse.Namespace] = None) -> bool:
    """
    Checks if single-run mode is active via --once CLI flag or RUN_ONCE environment variable.
    """
    env_run_once = os.getenv("RUN_ONCE", "").strip().lower() in ("true", "1", "yes")
    cli_once = bool(args and args.once)
    return cli_once or env_run_once


def poll_cycle(seen_store: SeenStore) -> None:
    """
    Executes a single polling iteration:
    1. Fetches latest posts from Reddit Atom RSS feed.
    2. Silently marks seen if initial run with empty DB and backlog alert disabled.
    3. Checks deduplication, matches keywords, sends Telegram alerts, and saves seen posts.
    """
    logger.info("Polling Reddit RSS feed...")
    posts = fetch_new_posts()

    # Process posts oldest-first so alerts arrive in chronological order
    posts_to_process = list(reversed(posts))

    # Check if this is the initial run with an empty database
    if seen_store.is_empty() and not config.ALERT_ON_FIRST_RUN_BACKLOG:
        logger.info(
            "First run with empty database: silently marking %d existing posts as seen without alerts.",
            len(posts_to_process),
        )
        for post in posts_to_process:
            seen_store.add(post.id)
    else:
        for post in posts_to_process:
            if not running:
                break
            process(post, seen_store)


def run_radar(once: Optional[bool] = None) -> None:
    """Main listening and polling loop or single-pass execution."""
    if once is None:
        once = is_single_run_mode()

    seen_store = SeenStore(config.DB_PATH)

    logger.info("Starting GhostLead Radar (RSS Mode)...")
    logger.info("Database: %s", config.DB_PATH)
    logger.info("Feed URL: %s", FEED_URL)
    logger.info("Subreddits: %s", ", ".join(config.SUBREDDITS))
    logger.info("Keywords configured: %d", len(config.KEYWORDS))

    if once:
        logger.info("Single-run execution mode active (--once / RUN_ONCE).")
        try:
            poll_cycle(seen_store)
            logger.info("Single-run execution finished successfully.")
        except requests.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else None
            logger.warning("Reddit HTTP error %s during single-run: %s", status_code, exc)
        except ET.ParseError as exc:
            logger.warning("XML parse error during single-run: %s", exc)
        except requests.RequestException as exc:
            logger.warning("Reddit request network error during single-run: %s", exc)
        except Exception:
            logger.exception("Unexpected error during single-run poll.")
        return

    logger.info(
        "Polling interval: %d seconds. Fetch limit: %d",
        config.POLL_INTERVAL,
        config.FETCH_LIMIT,
    )

    backoff = float(config.POLL_INTERVAL)

    while running:
        try:
            poll_cycle(seen_store)

            # Successful cycle: reset backoff to configured poll interval
            backoff = float(config.POLL_INTERVAL)

            if running:
                sleep_interruptible(config.POLL_INTERVAL)

        except requests.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else None
            if status_code == 429:
                retry_after = None
                if exc.response is not None and "Retry-After" in exc.response.headers:
                    ra_val = exc.response.headers.get("Retry-After", "").strip()
                    if ra_val.isdigit():
                        retry_after = float(ra_val)

                if retry_after is not None:
                    logger.warning(
                        "Reddit rate limited (HTTP 429). Retrying after %.1fs (Retry-After header)...",
                        retry_after,
                    )
                    sleep_interruptible(retry_after)
                else:
                    logger.warning(
                        "Reddit rate limited (HTTP 429). Backing off for %.1fs...",
                        backoff,
                    )
                    sleep_interruptible(backoff)
                    backoff = min(backoff * 2, 900.0)

            elif status_code == 403:
                logger.error("Reddit 403 (IP/UA block possible)")
                sleep_interruptible(backoff)
                backoff = min(backoff * 2, 900.0)

            else:
                logger.warning(
                    "Reddit HTTP error %s: %s. Backing off for %.1fs...",
                    status_code,
                    exc,
                    backoff,
                )
                sleep_interruptible(backoff)
                backoff = min(backoff * 2, 900.0)

        except ET.ParseError as exc:
            logger.warning(
                "XML parse error: %s (Reddit may have returned an HTML error page). Backing off for %.1fs...",
                exc,
                backoff,
            )
            sleep_interruptible(backoff)
            backoff = min(backoff * 2, 900.0)

        except requests.RequestException as exc:
            logger.warning(
                "Reddit request network error: %s. Backing off for %.1fs...",
                exc,
                backoff,
            )
            sleep_interruptible(backoff)
            backoff = min(backoff * 2, 900.0)

        except Exception:
            logger.exception(
                "Unexpected error in polling loop. Backing off for %.1fs...",
                backoff,
            )
            sleep_interruptible(backoff)
            backoff = min(backoff * 2, 900.0)

    logger.info("GhostLead Radar shutdown complete.")


def main(argv: Optional[List[str]] = None) -> None:
    # Register signal handlers for graceful shutdown on SIGINT and SIGTERM
    signal.signal(signal.SIGINT, handle_shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handle_shutdown)

    args = parse_arguments(argv)
    once = is_single_run_mode(args)

    run_radar(once=once)
    sys.exit(0)


if __name__ == "__main__":
    main()
