import time
import json
from pathlib import Path
from datetime import datetime, timezone
import os

import requests
import feedparser

# ── Credentials ────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")
SOCIALDATA_API_KEY = os.environ.get("SOCIALDATA_API_KEY", "")

# ── WordPress RSS config ────────────────────────────────────────
FEED_URL = "https://jobs.wordpress.net/feed/"
STATE_FILE = "last_seen_job.json"

# ── SocialData/Twitter config ───────────────────────────────────
X_STATE_FILE = "last_seen_tweet.json"
X_KEYWORDS = [
    "looking for wordpress developer",
    "need wordpress developer",
    "hire website developer",
    "wordpress developer needed",
    "wordpress freelancer",
    "looking for shopify developer",
    "need shopify developer",
    "hire shopify developer",
]

# ── General ─────────────────────────────────────────────────────
CHECK_INTERVAL = 600  # 10 minutes
SEEN_TWEET_IDS: set = set()


# ── Telegram ────────────────────────────────────────────────────
def send_telegram_message(text: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
    }
    response = requests.post(url, data=payload, timeout=20)
    response.raise_for_status()


# ── WordPress RSS ────────────────────────────────────────────────
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
        print("First run: WordPress baseline saved.")
        return

    if latest_link == last_seen:
        print("No new WordPress jobs.")
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
            return

    save_last_seen(latest_link)


# ── SocialData Twitter Search ────────────────────────────────────
def load_seen_ids():
    path = Path(X_STATE_FILE)
    if not path.exists():
        return set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return set(data.get("seen_ids", []))
    except Exception:
        return set()


def save_seen_ids(seen_ids: set):
    # keep only the last 500 IDs so the file doesn't grow forever
    ids_list = list(seen_ids)[-500:]
    with open(X_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"seen_ids": ids_list}, f)


def build_query():
    parts = [f'"{kw}"' for kw in X_KEYWORDS]
    return "(" + " OR ".join(parts) + ") -is:retweet lang:en"


def search_tweets():
    if not SOCIALDATA_API_KEY:
        print("SOCIALDATA_API_KEY not set, skipping Twitter search.")
        return []

    url = "https://api.socialdata.tools/twitter/search"
    headers = {
        "Authorization": f"Bearer {SOCIALDATA_API_KEY}",
        "Accept": "application/json",
    }
    params = {
        "query": build_query(),
        "type": "Latest",
    }

    response = requests.get(url, headers=headers, params=params, timeout=30)

    if response.status_code == 402:
        print("SocialData: insufficient credits.")
        return []
    if response.status_code == 429:
        print("SocialData: rate limited. Skipping this cycle.")
        return []

    response.raise_for_status()
    data = response.json()

    tweets = data.get("tweets") or []
    if not tweets:
        return []

    # filter to only tweets posted within the last 15 minutes
    now = time.time()
    recent_tweets = []
    for tweet in tweets:
        created_at = tweet.get("tweet_created_at", "")
        try:
            dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            age_minutes = (now - dt.timestamp()) / 60
            if age_minutes <= 15:
                recent_tweets.append(tweet)
        except Exception:
            continue  # skip tweets with unparseable dates

    return recent_tweets


def check_new_tweets():
    global SEEN_TWEET_IDS

    if not SOCIALDATA_API_KEY:
        return

    tweets = search_tweets()

    if not tweets:
        print("No recent tweets found.")
        return

    sent = 0
    for tweet in tweets:
        tweet_id = tweet.get("id_str", "")

        if tweet_id in SEEN_TWEET_IDS:
            continue

        username = (tweet.get("user") or {}).get("screen_name", "unknown")
        text = tweet.get("full_text") or tweet.get("text", "")
        link = f"https://twitter.com/{username}/status/{tweet_id}"
        created_at = tweet.get("tweet_created_at", "")

        message = (
            f"🐦 New Lead on X/Twitter\n\n"
            f"@{username}\n"
            f"Posted: {created_at}\n\n"
            f"{text}\n\n"
            f"Link: {link}"
        )

        try:
            send_telegram_message(message)
            SEEN_TWEET_IDS.add(tweet_id)
            sent += 1
            print(f"Sent tweet from @{username}")
        except Exception as e:
            print(f"Failed to send tweet {tweet_id}: {e}")
            break  # don't skip ahead on send failure

    save_seen_ids(SEEN_TWEET_IDS)

    if sent == 0:
        print("No new tweets to send.")


# ── Main ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    SEEN_TWEET_IDS = load_seen_ids()

    print("JobRadar starting...")
    print(f"BOT_TOKEN set: {bool(BOT_TOKEN)}")
    print(f"CHAT_ID set: {bool(CHAT_ID)}")
    print(f"SOCIALDATA_API_KEY set: {bool(SOCIALDATA_API_KEY)}")

    try:
        send_telegram_message(
            "✅ JobRadar is live!\n\n"
            "Watching:\n"
            "• WordPress Jobs RSS\n"
            "• Twitter/X via SocialData (last 15 mins only)"
        )
        print("Startup message sent.")
    except Exception as e:
        print(f"Failed to send startup message: {e}")

    while True:
        try:
            check_new_jobs()
        except Exception as e:
            print(f"WordPress feed error: {e}")

        try:
            check_new_tweets()
        except Exception as e:
            print(f"Twitter search error: {e}")

        time.sleep(CHECK_INTERVAL)