import os
import praw
import sqlite3
import requests
import imagehash
import time
from PIL import Image
from io import BytesIO

# --- CONFIGURATION (Pulls from Railway Variables) ---
CLIENT_ID = os.environ.get('REDDIT_CLIENT_ID')
CLIENT_SECRET = os.environ.get('REDDIT_SECRET')
USERNAME = os.environ.get('REDDIT_USERNAME')
PASSWORD = os.environ.get('REDDIT_PASSWORD')
USER_AGENT = "Windows11:AntiMemes Repost Detection:v1.0 (by /u/Curry__Fan)"

TARGET_SUBREDDIT = "AntiMemes"
SCAN_SUBREDDITS = ["AntiMemes", "antimeme"]
SIMILARITY_THRESHOLD = 3 
DAYS_TO_BACKFILL = 365

# --- DATABASE SETUP ---
DB_PATH = "/data/antimeme_index.db"
if not os.path.exists("/data"):
    DB_PATH = "antimeme_index.db"

db = sqlite3.connect(DB_PATH)
cursor = db.cursor()
cursor.execute("CREATE TABLE IF NOT EXISTS posts (hash TEXT PRIMARY KEY, link TEXT, timestamp REAL)")
db.commit()

reddit = praw.Reddit(
    client_id=CLIENT_ID,
    client_secret=CLIENT_SECRET,
    password=PASSWORD,
    user_agent=USER_AGENT,
    username=USERNAME
)

def get_image_hash(url):
    if not any(url.lower().endswith(ext) for ext in ['.jpg', '.jpeg', '.png']):
        return None
    try:
        response = requests.get(url, timeout=10)
        img = Image.open(BytesIO(response.content))
        return str(imagehash.phash(img))
    except:
        return None

def run_backfill():
    print(f"Starting backfill...")
    start_time = int(time.time()) - (DAYS_TO_BACKFILL * 86400)
    for sub in SCAN_SUBREDDITS:
        # FIXED URL PATH BELOW
        url = f"https://pullpush.io{sub}&after={start_time}&size=100"
        while True:
            try:
                res = requests.get(url, timeout=15).json()
                data = res.get('data', [])
                if not data: break
                for post in data:
                    h = get_image_hash(post.get('url', ''))
                    if h:
                        cursor.execute("INSERT OR IGNORE INTO posts VALUES (?, ?, ?)", 
                                       (h, post['permalink'], post['created_utc']))
                last_time = data[-1]['created_utc']
                url = f"https://pullpush.io{sub}&after={last_time}&size=100"
                db.commit()
                print(f"Indexed r/{sub} up to {time.ctime(last_time)}")
            except: break
    print("Backfill complete.")

def run_bot():
    cursor.execute("SELECT COUNT(*) FROM posts")
    if cursor.fetchone()[0] == 0: # FIXED INDEX
        run_backfill()

    print(f"Monitoring r/{TARGET_SUBREDDIT}...")
    for submission in reddit.subreddit(TARGET_SUBREDDIT).stream.submissions(skip_existing=True):
        curr_hash_str = get_image_hash(submission.url)
        if not curr_hash_str: continue

        curr_hash = imagehash.hex_to_hash(curr_hash_str)
        cursor.execute("SELECT hash, link FROM posts")
        repost_link = None
        exact_match = False

        for old_hash_str, old_link in cursor.fetchall():
            diff = curr_hash - imagehash.hex_to_hash(old_hash_str)
            if diff <= SIMILARITY_THRESHOLD:
                repost_link = old_link
                if diff == 0: exact_match = True
                break

        if repost_link:
            submission.reply(f"Sorry, you've already posted this: https://reddit.com{repost_link}")
            submission.mod.remove()
            print(f"Removed repost {submission.id}")
        
        if not exact_match:
            cursor.execute("INSERT OR REPLACE INTO posts VALUES (?, ?, ?)", 
                           (curr_hash_str, submission.permalink, submission.created_utc))
            db.commit()

if __name__ == "__main__":
    run_bot()
