import time
import json
from pathlib import Path
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
    "hire wordpress developer",
    "wordpress developer needed",
    "wordpress freelancer",
    "looking for shopify developer",
    "need shopify developer",
    "hire shopify developer",
]

# ── General ─────────────────────────────────────────────────────
CHECK_INTERVAL = 600  # 10 minutes


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
def load_last_tweet_id():
    path = Path(X_STATE_FILE)
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("last_tweet_id")
    except Exception:
        return None


def save_last_tweet_id(tweet_id: str):
    with open(X_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"last_tweet_id": tweet_id}, f)


def build_query():
    parts = [f'"{kw}"' for kw in X_KEYWORDS]
    return "(" + " OR ".join(parts) + ") -is:retweet lang:en"


def search_tweets(since_id=None):
    if not SOCIALDATA_API_KEY:
        print("SOCIALDATA_API_KEY not set, skipping Twitter search.")
        return [], None

    url = "https://api.socialdata.tools/twitter/search"
    headers = {
        "Authorization": f"Bearer {SOCIALDATA_API_KEY}",
        "Accept": "application/json",
    }

    query = build_query()
    if since_id:
        query += f" since_id:{since_id}"

    params = {
        "query": query,
        "type": "Latest",
    }

    response = requests.get(url, headers=headers, params=params, timeout=30)

    if response.status_code == 402:
        print("SocialData: insufficient credits.")
        return [], None

    if response.status_code == 429:
        print("SocialData: rate limited. Skipping this cycle.")
        return [], None

    response.raise_for_status()
    data = response.json()

    tweets = data.get("tweets") or []
    if not tweets:
        return [], None

    # sort oldest first
    tweets_sorted = sorted(tweets, key=lambda t: int(t.get("id_str", "0")))

    results = []
    for tweet in tweets_sorted:
        results.append({
            "id": tweet.get("id_str", ""),
            "text": tweet.get("full_text") or tweet.get("text", ""),
            "username": (tweet.get("user") or {}).get("screen_name", "unknown"),
            "created_at": tweet.get("tweet_created_at", ""),
        })

    max_id = results[-1]["id"] if results else None
    return results, max_id


def check_new_tweets():
    if not SOCIALDATA_API_KEY:
        return

    since_id = load_last_tweet_id()
    tweets, max_id = search_tweets(since_id=since_id)

    # first run — save baseline only, send nothing
    if since_id is None:
        if max_id:
            save_last_tweet_id(max_id)
            print("First run: Twitter baseline saved.")
        return

    if not tweets:
        print("No new tweets found.")
        return

    for tweet in tweets:
        username = tweet["username"]
        text = tweet["text"]
        tweet_id = tweet["id"]
        link = f"https://twitter.com/{username}/status/{tweet_id}"

        message = (
            f"🐦 New Lead on X/Twitter\n\n"
            f"@{username}\n\n"
            f"{text}\n\n"
            f"Link: {link}"
        )

        try:
            send_telegram_message(message)
            print(f"Sent tweet from @{username}")
        except Exception as e:
            print(f"Failed to send tweet {tweet_id}: {e}")
            return  # don't advance since_id past unsent tweets

    if max_id:
        save_last_tweet_id(max_id)


# ── Main ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("JobRadar starting...")
    print(f"BOT_TOKEN set: {bool(BOT_TOKEN)}")
    print(f"CHAT_ID set: {bool(CHAT_ID)}")
    print(f"SOCIALDATA_API_KEY set: {bool(SOCIALDATA_API_KEY)}")

    try:
        send_telegram_message(
            "✅ JobRadar is live!\n\n"
            "Watching:\n"
            "• WordPress Jobs RSS\n"
            "• Twitter/X via SocialData"
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