import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
from dotenv import load_dotenv
from slack_sdk import WebClient


load_dotenv()

# ---------- Config ----------

ED_REGION = os.getenv("ED_REGION", "us").strip()
ED_API_TOKEN = os.environ["ED_API_TOKEN"].strip()
ED_COURSE_ID = os.environ["ED_COURSE_ID"].strip()

SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"].strip()
ED_LEAD_SLACK_ID = os.environ["ED_LEAD_SLACK_ID"].strip()

# Alert only for unresolved posts between these ages.
ALERT_AFTER_HOURS = int(os.getenv("ALERT_AFTER_HOURS", "8"))
ALERT_MAX_AGE_DAYS = int(os.getenv("ALERT_MAX_AGE_DAYS", "7"))

# Fetch all Ed threads page by page, then keep the latest 100 by post number.
FETCH_LIMIT = int(os.getenv("FETCH_LIMIT", "100"))
FETCH_PAGE_SIZE = int(os.getenv("FETCH_PAGE_SIZE", "100"))

# Keep checking forever.
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "300"))

DB_PATH = os.getenv("DB_PATH", "alerts.db").strip()

# For debugging.
DEBUG_PRINT_THREADS = os.getenv("DEBUG_PRINT_THREADS", "false").strip().lower() == "true"

# Most setups use Bearer.
# If yours only worked without Bearer, set ED_AUTH_STYLE=raw in .env.
ED_AUTH_STYLE = os.getenv("ED_AUTH_STYLE", "bearer").strip().lower()


# ---------- Helpers ----------

def ed_headers() -> dict[str, str]:
    if ED_AUTH_STYLE == "raw":
        authorization = ED_API_TOKEN
    else:
        authorization = f"Bearer {ED_API_TOKEN}"

    return {
        "Authorization": authorization,
        "Accept": "application/json",
    }


def parse_ed_time(value: Any) -> datetime:
    """
    Handles common Ed timestamp formats:
    - ISO string, e.g. 2026-06-16T13:45:00Z
    - ISO string with offset
    - Unix timestamp in seconds
    """
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)

    if not isinstance(value, str):
        raise ValueError(f"Unsupported timestamp: {value!r}")

    value = value.strip()

    if value.isdigit():
        return datetime.fromtimestamp(int(value), tz=timezone.utc)

    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except Exception:
        return default


def slack_escape(value: Any) -> str:
    """
    Escapes text for Slack messages.
    """
    text = str(value)
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def get_category_name(thread: dict[str, Any]) -> str:
    category = thread.get("category")

    if isinstance(category, dict):
        return str(category.get("name", "Uncategorized"))

    if category:
        return str(category)

    return "Uncategorized"


# ---------- Database ----------

def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS alerted_threads (
                thread_id TEXT PRIMARY KEY,
                thread_number TEXT,
                title TEXT,
                first_alerted_at TEXT NOT NULL,
                last_alerted_at TEXT NOT NULL,
                alert_count INTEGER NOT NULL DEFAULT 1
            )
            """
        )


def already_alerted(thread_id: str) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT 1 FROM alerted_threads WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()

    return row is not None


def mark_alerted(thread: dict[str, Any]) -> None:
    now = datetime.now(timezone.utc).isoformat()

    thread_id = str(thread.get("id"))
    thread_number = str(thread.get("number", thread_id))
    title = str(thread.get("title", ""))

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO alerted_threads
                (thread_id, thread_number, title, first_alerted_at, last_alerted_at, alert_count)
            VALUES
                (?, ?, ?, ?, ?, 1)
            ON CONFLICT(thread_id) DO UPDATE SET
                last_alerted_at = excluded.last_alerted_at,
                alert_count = alert_count + 1
            """,
            (thread_id, thread_number, title, now, now),
        )


# ---------- Ed API ----------
def fetch_ed_threads(limit: int = FETCH_LIMIT) -> list[dict[str, Any]]:
    """
    Fetches all Ed threads using pagination.

    Then sorts all fetched threads by Ed post number from greatest to least,
    and returns only the top `limit`.

    Example order:
    #500, #499, #498, ...
    """
    url = f"https://{ED_REGION}.edstem.org/api/courses/{ED_COURSE_ID}/threads"

    all_threads: list[dict[str, Any]] = []
    seen_thread_ids: set[str] = set()
    offset = 0

    while True:
        response = requests.get(
            url,
            headers=ed_headers(),
            params={
                "limit": FETCH_PAGE_SIZE,
                "offset": offset,
            },
            timeout=30,
        )

        if response.status_code != 200:
            print("Ed API error")
            print("Status:", response.status_code)
            print("Response:", response.text[:2000])
            response.raise_for_status()

        data = response.json()

        if not isinstance(data, dict):
            raise RuntimeError(f"Unexpected Ed response shape: {type(data)}")

        threads = data.get("threads", [])

        if not isinstance(threads, list):
            raise RuntimeError(f"Unexpected threads shape: {type(threads)}")

        if not threads:
            print(f"No threads returned at offset={offset}. Finished fetching pages.")
            break

        new_threads_this_page = 0

        for thread in threads:
            thread_id = str(thread.get("id"))

            if not thread_id or thread_id == "None":
                continue

            if thread_id in seen_thread_ids:
                continue

            seen_thread_ids.add(thread_id)
            all_threads.append(thread)
            new_threads_this_page += 1

        print(
            f"Fetched page offset={offset}, "
            f"page_threads={len(threads)}, "
            f"new_unique_threads={new_threads_this_page}, "
            f"total_unique_threads={len(all_threads)}"
        )

        if len(threads) < FETCH_PAGE_SIZE:
            print("Last page was smaller than page size. Finished fetching pages.")
            break

        if new_threads_this_page == 0:
            print("No new unique threads found on this page. Stopping pagination.")
            break

        offset += FETCH_PAGE_SIZE

    all_threads.sort(
        key=lambda thread: safe_int(thread.get("number"), 0),
        reverse=True,
    )

    return all_threads[:limit]


def is_question(thread: dict[str, Any]) -> bool:
    return thread.get("type") == "question"


def is_deleted_or_archived(thread: dict[str, Any]) -> bool:
    return bool(thread.get("deleted_at")) or bool(thread.get("is_archived"))


def is_old_enough(thread: dict[str, Any], stale_cutoff: datetime) -> bool:
    created_at_raw = thread.get("created_at")

    if not created_at_raw:
        return False

    created_at = parse_ed_time(created_at_raw)
    return created_at <= stale_cutoff


def is_not_too_old(thread: dict[str, Any], oldest_cutoff: datetime) -> bool:
    created_at_raw = thread.get("created_at")

    if not created_at_raw:
        return False

    created_at = parse_ed_time(created_at_raw)
    return created_at >= oldest_cutoff


def is_unresolved(thread: dict[str, Any]) -> bool:
    """
    Alert only if the Ed post itself is not resolved/answered.
    Ignores unresolved comments and follow-ups.
    """
    is_answered = bool(thread.get("is_answered", False))
    return not is_answered


def find_unresolved_threads(threads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc)

    stale_cutoff = now - timedelta(hours=ALERT_AFTER_HOURS)
    oldest_cutoff = now - timedelta(days=ALERT_MAX_AGE_DAYS)

    unresolved_threads = []

    for thread in threads:
        thread_id = str(thread.get("id"))

        if not thread_id or thread_id == "None":
            continue

        # Prevent repeat notifications.
        if already_alerted(thread_id):
            continue

        if not is_question(thread):
            continue

        if is_deleted_or_archived(thread):
            continue

        # Must be at least ALERT_AFTER_HOURS old.
        if not is_old_enough(thread, stale_cutoff):
            continue

        # Must not be older than ALERT_MAX_AGE_DAYS.
        if not is_not_too_old(thread, oldest_cutoff):
            continue

        # Only posts that are not resolved/answered.
        if not is_unresolved(thread):
            continue

        unresolved_threads.append(thread)

    return unresolved_threads


# ---------- Debugging ----------

def debug_print_threads(threads: list[dict[str, Any]]) -> None:
    if not DEBUG_PRINT_THREADS:
        return

    print("\n--- Top threads by number, greatest to least ---")

    for index, thread in enumerate(threads, start=1):
        thread_id = thread.get("id")
        thread_number = thread.get("number", thread_id)
        title = thread.get("title", "(untitled question)")
        thread_type = thread.get("type")
        created_at = thread.get("created_at")
        is_answered = thread.get("is_answered")
        deleted_at = thread.get("deleted_at")
        is_archived = thread.get("is_archived")

        already_seen = already_alerted(str(thread_id)) if thread_id else False

        print(
            f"{index}. #{thread_number} | "
            f"id={thread_id} | "
            f"type={thread_type} | "
            f"created_at={created_at} | "
            f"is_answered={is_answered} | "
            f"deleted_at={deleted_at} | "
            f"is_archived={is_archived} | "
            f"already_alerted={already_seen} | "
            f"title={title}"
        )

    print("--- End top threads ---\n")


# ---------- Slack ----------

def ed_thread_url(thread: dict[str, Any]) -> str:
    """
    Ed links should use the discussion ID, not the displayed post number.
    """
    thread_id = thread.get("id")
    return f"https://edstem.org/{ED_REGION}/courses/{ED_COURSE_ID}/discussion/{thread_id}"


def format_thread_line(thread: dict[str, Any]) -> str:
    thread_number = thread.get("number", thread.get("id"))
    title = slack_escape(thread.get("title", "(untitled question)"))
    url = ed_thread_url(thread)

    created_at = parse_ed_time(thread["created_at"])
    age_hours = int((datetime.now(timezone.utc) - created_at).total_seconds() // 3600)

    reply_count = safe_int(thread.get("reply_count"), 0)
    category = slack_escape(get_category_name(thread))

    return (
        f"• <{url}|#{thread_number}: {title}> "
        f"— {age_hours}h old, {reply_count} replies, "
        f"category: {category}"
    )


def open_dm_with_lead(client: WebClient) -> str:
    """
    Opens or resumes a DM between the bot and the Ed lead.
    Returns the DM channel ID, which usually starts with D.
    """
    response = client.conversations_open(
        users=ED_LEAD_SLACK_ID,
        return_im=True,
    )

    return response["channel"]["id"]


def post_slack_alert(unresolved_threads: list[dict[str, Any]]) -> None:
    client = WebClient(token=SLACK_BOT_TOKEN)

    dm_channel_id = open_dm_with_lead(client)

    shown_threads = unresolved_threads[:10]
    thread_lines = "\n".join(format_thread_line(thread) for thread in shown_threads)

    extra = ""
    if len(unresolved_threads) > 10:
        extra = f"\n…and {len(unresolved_threads) - 10} more."

    text = (
        f"Hi <@{ED_LEAD_SLACK_ID}> — there are {len(unresolved_threads)} Ed question(s) "
        f"between {ALERT_AFTER_HOURS} hours and {ALERT_MAX_AGE_DAYS} days old "
        f"that are not resolved:\n\n"
        f"{thread_lines}"
        f"{extra}"
    )

    client.chat_postMessage(
        channel=dm_channel_id,
        text=text,
        unfurl_links=False,
    )


# ---------- Main loop ----------

def run_check_once() -> None:
    print(f"[{datetime.now(timezone.utc).isoformat()}] Fetching all Ed threads...")

    threads = fetch_ed_threads(limit=FETCH_LIMIT)

    print(f"Using top {len(threads)} thread(s) by greatest post number.")
    debug_print_threads(threads)

    unresolved_threads = find_unresolved_threads(threads)

    if not unresolved_threads:
        print("No new unresolved Ed questions in the alert window.")
        return

    print(f"Found {len(unresolved_threads)} unresolved question(s). DMing Ed lead...")

    # Only mark as alerted after the Slack message succeeds.
    post_slack_alert(unresolved_threads)

    for thread in unresolved_threads:
        mark_alerted(thread)

    print(f"Sent Slack DM and marked {len(unresolved_threads)} thread(s) as alerted.")


def main() -> None:
    init_db()

    print("Starting Ed unresolved-question monitor.")
    print(f"Checking every {CHECK_INTERVAL_SECONDS} seconds.")
    print(f"Alert window: {ALERT_AFTER_HOURS} hours old to {ALERT_MAX_AGE_DAYS} days old.")
    print(f"Fetching all Ed threads, then keeping top {FETCH_LIMIT} by greatest post number.")
    print(f"Fetch page size: {FETCH_PAGE_SIZE}.")
    print("Press Ctrl+C to stop.")

    while True:
        try:
            run_check_once()
        except KeyboardInterrupt:
            print("\nStopped Ed unresolved-question monitor.")
            break
        except Exception as error:
            print("Error during check:")
            print(repr(error))
            print("Continuing after sleep...")

        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()