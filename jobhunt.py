import time
import json
import threading
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

X_KEYWORD_BATCHES = [
    [
        "wordpress developer needed",
        "need wordpress developer",
        "hire wordpress developer",
        "python developer",
    ],
    [
        "shopify developer needed",
        "need shopify developer",
        "hire shopify developer",
        "shopify freelancer",
    ],
    [
        "web developer needed",
        "looking for web developer",
        "website developer needed",
        "need a web developer",
    ],
    [
        "web developer",
        "need wordpress help",
        "wordpress website needed",
        "need shopify help",
    ],
]

# ── General ─────────────────────────────────────────────────────
CHECK_INTERVAL = 600        # 10 minutes
TWEET_MAX_AGE_MINUTES = 60
SEEN_TWEET_IDS: set = set()
_batch_index = 0

# ── Pause/Resume state ───────────────────────────────────────────
is_paused = False
_last_update_id = None      # tracks Telegram updates so we don't re-process old ones


# ── Telegram ────────────────────────────────────────────────────
def send_telegram_message(text: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text}
    response = requests.post(url, data=payload, timeout=20)
    response.raise_for_status()


def get_telegram_updates(offset=None):
    """Poll Telegram for new messages sent to the bot."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    params = {"timeout": 5, "allowed_updates": ["message"]}
    if offset:
        params["offset"] = offset
    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        return response.json().get("result", [])
    except Exception as e:
        print(f"Failed to fetch Telegram updates: {e}")
        return []


def process_telegram_commands():
    """Check for /pause, /resume, /status commands sent to the bot."""
    global is_paused, _last_update_id

    updates = get_telegram_updates(offset=_last_update_id)

    for update in updates:
        _last_update_id = update["update_id"] + 1  # advance offset so we don't re-read
        message = update.get("message", {})
        text = message.get("text", "").strip().lower()
        from_id = str(message.get("chat", {}).get("id", ""))

        # Only accept commands from the authorised CHAT_ID
        if from_id != str(CHAT_ID):
            continue

        if text == "/pause":
            if is_paused:
                send_telegram_message("⏸ JobRadar is already paused.")
            else:
                is_paused = True
                send_telegram_message(
                    "⏸ JobRadar paused.\n\nSend /resume to start again."
                )
                print("Bot paused via Telegram command.")

        elif text == "/resume":
            if not is_paused:
                send_telegram_message("▶️ JobRadar is already running.")
            else:
                is_paused = False
                send_telegram_message(
                    "▶️ JobRadar resumed!\n\nWatching for new jobs and leads."
                )
                print("Bot resumed via Telegram command.")

        elif text == "/status":
            state = "⏸ PAUSED" if is_paused else "▶️ RUNNING"
            send_telegram_message(
                f"JobRadar status: {state}\n\n"
                f"Commands:\n"
                f"/pause  — stop sending alerts\n"
                f"/resume — start sending alerts\n"
                f"/status — check current state"
            )


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
    ids_list = list(seen_ids)[-500:]
    with open(X_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"seen_ids": ids_list}, f)


def build_query(keywords: list) -> str:
    parts = [f'"{kw}"' for kw in keywords]
    return "(" + " OR ".join(parts) + ") -is:retweet lang:en"


def search_tweets(keywords: list) -> list:
    if not SOCIALDATA_API_KEY:
        print("SOCIALDATA_API_KEY not set, skipping Twitter search.")
        return []

    query = build_query(keywords)
    print(f"Querying: {query}")

    url = "https://api.socialdata.tools/twitter/search"
    headers = {
        "Authorization": f"Bearer {SOCIALDATA_API_KEY}",
        "Accept": "application/json",
    }
    params = {"query": query, "type": "Latest"}

    response = requests.get(url, headers=headers, params=params, timeout=30)

    if response.status_code == 402:
        print("SocialData: insufficient credits.")
        return []
    if response.status_code == 429:
        print("SocialData: rate limited. Skipping this cycle.")
        return []

    response.raise_for_status()
    data = response.json()

    tweets_raw = data.get("tweets") or []
    print(f"SocialData response: status={response.status_code}, tweets={len(tweets_raw)}, keys={list(data.keys())}")

    if not tweets_raw:
        return []

    now = time.time()
    recent_tweets = []
    for tweet in tweets_raw:
        created_at = tweet.get("tweet_created_at", "")
        try:
            dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            age_minutes = (now - dt.timestamp()) / 60
            if age_minutes <= TWEET_MAX_AGE_MINUTES:
                recent_tweets.append(tweet)
        except Exception:
            continue

    print(f"Tweets within {TWEET_MAX_AGE_MINUTES} mins: {len(recent_tweets)}")
    return recent_tweets


def check_new_tweets():
    global SEEN_TWEET_IDS, _batch_index

    if not SOCIALDATA_API_KEY:
        return

    keywords = X_KEYWORD_BATCHES[_batch_index % len(X_KEYWORD_BATCHES)]
    _batch_index += 1
    print(f"Checking keyword batch {_batch_index}: {keywords}")

    tweets = search_tweets(keywords)

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
            break

    save_seen_ids(SEEN_TWEET_IDS)

    if sent == 0:
        print("No new tweets to send.")


# ── Main loop ────────────────────────────────────────────────────
if __name__ == "__main__":
    SEEN_TWEET_IDS = load_seen_ids()

    print("JobRadar starting...")
    print(f"BOT_TOKEN set: {bool(BOT_TOKEN)}")
    print(f"CHAT_ID set: {bool(CHAT_ID)}")
    print(f"SOCIALDATA_API_KEY set: {bool(SOCIALDATA_API_KEY)}")
    print(f"Keyword batches: {len(X_KEYWORD_BATCHES)} (rotating each cycle)")

    try:
        send_telegram_message(
            "✅ JobRadar is live!\n\n"
            "Watching:\n"
            "• WordPress Jobs RSS\n"
            "• Twitter/X via SocialData (last 60 mins, rotating keyword batches)\n\n"
            "Commands:\n"
            "/pause  — stop sending alerts\n"
            "/resume — start sending alerts\n"
            "/status — check current state"
        )
        print("Startup message sent.")
    except Exception as e:
        print(f"Failed to send startup message: {e}")

    while True:
        # Always check for commands, even when paused
        try:
            process_telegram_commands()
        except Exception as e:
            print(f"Command polling error: {e}")

        if is_paused:
            print("Bot is paused. Skipping job and tweet checks.")
            time.sleep(CHECK_INTERVAL)
            continue

        try:
            check_new_jobs()
        except Exception as e:
            print(f"WordPress feed error: {e}")

        try:
            check_new_tweets()
        except Exception as e:
            print(f"Twitter search error: {e}")

        time.sleep(CHECK_INTERVAL)