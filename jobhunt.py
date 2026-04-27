import time
import json
from pathlib import Path
import os

import requests
import feedparser
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")
FEED_URL = "https://jobs.wordpress.net/feed/"
STATE_FILE = "last_seen_job.json"
CHECK_INTERVAL = 300  # 5 minutes


def send_telegram_message(text: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
    }
    response = requests.post(url, data=payload, timeout=20)
    response.raise_for_status()


def load_last_seen():
    path = Path(STATE_FILE)
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("last_link")
    except Exception:
        return None


def save_last_seen(link: str):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"last_link": link}, f)


def get_feed_entries():
    feed = feedparser.parse(FEED_URL)
    if feed.bozo and not feed.entries:
        raise ValueError(f"Feed error: {feed.bozo_exception}")
    return feed.entries if feed.entries else []


def check_new_jobs():
    entries = get_feed_entries()

    if not entries:
        print("No jobs found in feed.")
        return

    latest_entry = entries[0]
    latest_link = latest_entry.link
    last_seen = load_last_seen()

    if last_seen is None:
        save_last_seen(latest_link)
        print("First run: baseline saved.")
        return

    if latest_link == last_seen:
        print("No new jobs.")
        return

    new_entries = []
    for entry in entries:
        if entry.link == last_seen:
            break
        new_entries.append(entry)

    if not new_entries:
        save_last_seen(latest_link)
        return

    for entry in reversed(new_entries):
        title = entry.title
        link = entry.link
        published = getattr(entry, "published", "No date")

        message = (
            f"🆕 New WordPress Job Posted\n\n"
            f"Title: {title}\n"
            f"Date: {published}\n"
            f"Link: {link}"
        )

        try:
            send_telegram_message(message)
            print(f"Sent: {title}")
        except Exception as e:
            print(f"Failed to send '{title}': {e}")
            return  # don't advance last_seen past unsent jobs

    save_last_seen(latest_link)


if __name__ == "__main__":
    print("Watching WordPress jobs feed...")

    while True:
        try:
            check_new_jobs()
        except Exception as e:
            print("Error:", e)

        time.sleep(CHECK_INTERVAL)