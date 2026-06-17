import os
import sqlite3
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

ALERT_AFTER_HOURS = int(os.getenv("ALERT_AFTER_HOURS", "8"))
DB_PATH = os.getenv("DB_PATH", "alerts.db").strip()

# Use the same auth style that worked in inspect_ed.py.
# Most setups use Bearer. If yours only worked without Bearer, set ED_AUTH_STYLE=raw in .env.
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

def fetch_ed_threads(limit: int = 100) -> list[dict[str, Any]]:
    """
    Fetches recent Ed threads. This is enough for most courses if the script runs frequently.
    If your course gets more than 100 posts between runs, increase limit or add pagination.
    """
    url = f"https://{ED_REGION}.edstem.org/api/courses/{ED_COURSE_ID}/threads"

    response = requests.get(
        url,
        headers=ed_headers(),
        params={
            "limit": limit,
            "offset": 0,
        },
        timeout=30,
    )

    if response.status_code != 200:
        print("Ed API error")
        print("Status:", response.status_code)
        print("Response:", response.text[:2000])
        response.raise_for_status()

    data = response.json()

    if isinstance(data, dict):
        return data.get("threads", [])

    raise RuntimeError(f"Unexpected Ed response shape: {type(data)}")


def is_question(thread: dict[str, Any]) -> bool:
    return thread.get("type") == "question"


def is_deleted_or_archived(thread: dict[str, Any]) -> bool:
    return bool(thread.get("deleted_at")) or bool(thread.get("is_archived"))


def is_old_enough(thread: dict[str, Any], cutoff: datetime) -> bool:
    created_at_raw = thread.get("created_at")

    if not created_at_raw:
        return False

    created_at = parse_ed_time(created_at_raw)
    return created_at <= cutoff


def is_unanswered_or_unresolved(thread: dict[str, Any]) -> bool:
    """
    Main rule:
    Alert if a question is not answered, not staff answered, or has unresolved followups.
    """
    is_answered = bool(thread.get("is_answered", False))
    is_staff_answered = bool(thread.get("is_staff_answered", False))
    unresolved_count = safe_int(thread.get("unresolved_count"), 0)

    unanswered = not is_answered
    not_staff_answered = not is_staff_answered
    has_unresolved_followups = unresolved_count > 0

    return unanswered or not_staff_answered or has_unresolved_followups


def find_stale_threads(threads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=ALERT_AFTER_HOURS)

    stale = []

    for thread in threads:
        thread_id = str(thread.get("id"))

        if not thread_id or thread_id == "None":
            continue

        if already_alerted(thread_id):
            continue

        if not is_question(thread):
            continue

        if is_deleted_or_archived(thread):
            continue

        if not is_old_enough(thread, cutoff):
            continue

        if not is_unanswered_or_unresolved(thread):
            continue

        stale.append(thread)

    return stale


# ---------- Slack ----------

def ed_thread_url(thread: dict[str, Any]) -> str:
    thread_number = thread.get("number", thread.get("id"))
    return f"https://edstem.org/{ED_REGION}/courses/{ED_COURSE_ID}/discussion/{thread_number}"


def format_thread_line(thread: dict[str, Any]) -> str:
    thread_number = thread.get("number", thread.get("id"))
    title = thread.get("title", "(untitled question)")
    url = ed_thread_url(thread)

    created_at = parse_ed_time(thread["created_at"])
    age_hours = int((datetime.now(timezone.utc) - created_at).total_seconds() // 3600)

    reply_count = safe_int(thread.get("reply_count"), 0)
    unresolved_count = safe_int(thread.get("unresolved_count"), 0)
    category = thread.get("category") or "Uncategorized"

    return (
        f"• <{url}|#{thread_number}: {title}> "
        f"— {age_hours}h old, {reply_count} replies, "
        f"{unresolved_count} unresolved, category: {category}"
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


def post_slack_alert(stale_threads: list[dict[str, Any]]) -> None:
    client = WebClient(token=SLACK_BOT_TOKEN)

    dm_channel_id = open_dm_with_lead(client)

    shown_threads = stale_threads[:10]
    thread_lines = "\n".join(format_thread_line(thread) for thread in shown_threads)

    extra = ""
    if len(stale_threads) > 10:
        extra = f"\n…and {len(stale_threads) - 10} more."

    text = (
        f"Hi <@{ED_LEAD_SLACK_ID}> — there are {len(stale_threads)} Ed question(s) "
        f"older than {ALERT_AFTER_HOURS} hours that still look unanswered or unresolved:\n\n"
        f"{thread_lines}"
        f"{extra}"
    )

    client.chat_postMessage(
        channel=dm_channel_id,
        text=text,
        unfurl_links=False,
    )


# ---------- Main ----------

def main() -> None:
    init_db()

    print("Fetching Ed threads...")
    threads = fetch_ed_threads(limit=100)

    print(f"Fetched {len(threads)} thread(s).")
    stale_threads = find_stale_threads(threads)

    if not stale_threads:
        print("No new stale unanswered/unresolved Ed questions.")
        return

    print(f"Found {len(stale_threads)} stale question(s). DMing Ed lead...")
    post_slack_alert(stale_threads)

    for thread in stale_threads:
        mark_alerted(thread)

    print(f"Sent Slack DM and marked {len(stale_threads)} thread(s) as alerted.")


if __name__ == "__main__":
    main()
