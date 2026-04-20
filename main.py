import os
import praw
import sqlite3
import requests
import imagehash
import time
from datetime import datetime
from PIL import Image
from io import BytesIO
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# --- CONFIGURATION ---
# These are pulled from Railway Environment Variables
CLIENT_ID = os.environ.get('REDDIT_CLIENT_ID')
CLIENT_SECRET = os.environ.get('REDDIT_SECRET')
REFRESH_TOKEN = os.environ.get('REDDIT_REFRESH_TOKEN')
# Update the 'by /u/...' to your main account username
USER_AGENT = "script:AntiMemes Repost Detection:v1.5 (by /u/Curry__Fan)"

TARGET_SUBREDDIT = "AntiMemes"
SCAN_SUBREDDITS = ["AntiMemes", "antimeme"]
MONITOR_STR = "AntiMemes+antimeme"

SIMILARITY_THRESHOLD = 4
# Path optimized for Railway Volume mount at /data
DB_PATH = "/data/antimeme_index.db" if os.path.exists("/data") else "antimeme_index.db"
RETENTION_DAYS = 365

# --- NETWORKING & DB ---
session = requests.Session()
retries = Retry(total=5, backoff_factor=2, status_forcelist=[429, 500, 502, 503, 504])
session.mount('https://', HTTPAdapter(max_retries=retries))

def get_db():
    """Initializes the database in the persistent /data directory."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS posts (hash TEXT PRIMARY KEY, link TEXT, timestamp REAL)")
    return conn

def get_reddit_instance():
    """Creates a PRAW instance using the Refresh Token (No password needed)."""
    return praw.Reddit(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        refresh_token=REFRESH_TOKEN,
        user_agent=USER_AGENT
    )

def get_image_hashes(url_or_submission):
    urls = []
    if isinstance(url_or_submission, str):
        urls.append(url_or_submission)
    else:
        if hasattr(url_or_submission, 'is_gallery') and url_or_submission.is_gallery:
            for item in url_or_submission.gallery_data.get('items', []):
                media_id = item['media_id']
                if media_id in url_or_submission.media_metadata:
                    urls.append(url_or_submission.media_metadata[media_id]['s']['u'])
        else:
            urls.append(url_or_submission.url)

    hashes = []
    for url in urls:
        if not any(ext in url.lower() for ext in ['.jpg', '.jpeg', '.png', '.webp']):
            continue
        try:
            response = session.get(url, timeout=10)
            img = Image.open(BytesIO(response.content))
            if getattr(img, "is_animated", False):
                img.seek(0)
            hashes.append(str(imagehash.phash(img)))
        except:
            continue
    return hashes

def run_backfill(cursor, db):
    print("Starting backfill process... This may take several hours.")
    start_time = int(time.time()) - (RETENTION_DAYS * 86400)
    
    for sub in SCAN_SUBREDDITS:
        print(f"Indexing historical posts from r/{sub}...")
        url = f"https://api.pullpush.io/reddit/search/submission/?subreddit={sub}&after={start_time}&size=100"
        
        while True:
            try:
                res = session.get(url, timeout=20).json()
                data = res.get('data', [])
                if not data:
                    break
                
                for post in data:
                    post_url = post.get('url', '')
                    hashes = get_image_hashes(post_url)
                    for h in hashes:
                        cursor.execute("INSERT OR IGNORE INTO posts VALUES (?, ?, ?)", 
                                     (h, post['permalink'], post['created_utc']))
                
                db.commit()
                last_time = data[-1]['created_utc']
                print(f"Indexed r/{sub} up to {datetime.fromtimestamp(last_time)}")
                url = f"https://api.pullpush.io/reddit/search/submission/?subreddit={sub}&after={int(last_time)}&size=100"
                time.sleep(1) 
            except Exception as e:
                print(f"Backfill batch failed: {e}. Finishing sub.")
                break
    print("Backfill complete. Database is primed.")

def check_for_repost(curr_hash_str, cursor):
    curr_hash = imagehash.hex_to_hash(curr_hash_str)
    cursor.execute("SELECT hash, link FROM posts")
    for old_hash_str, old_link in cursor.fetchall():
        old_hash = imagehash.hex_to_hash(old_hash_str)
        if (curr_hash - old_hash) <= SIMILARITY_THRESHOLD:
            return old_link
    return None

def get_mods(reddit, sub_name):
    try:
        return {mod.name for mod in reddit.subreddit(sub_name).moderator()}
    except:
        return set()

def run_bot():
    db = get_db()
    cursor = db.cursor()
    
    # Initial Backfill check
    cursor.execute("SELECT COUNT(*) FROM posts")
    if cursor.fetchone()[0] == 0:
        run_backfill(cursor, db)
    
    reddit = get_reddit_instance()
    print(f"Authenticated as: {reddit.user.me()}")
    
    last_cleanup_day = datetime.now().day
    mods = get_mods(reddit, TARGET_SUBREDDIT)
    
    print(f"Monitoring {MONITOR_STR} for new posts...")

    try:
        for submission in reddit.subreddit(MONITOR_STR).stream.submissions(skip_existing=True):
            current_now = datetime.now()
            if current_now.day != last_cleanup_day:
                cutoff = time.time() - (RETENTION_DAYS * 86400)
                cursor.execute("DELETE FROM posts WHERE timestamp < ?", (cutoff,))
                db.commit()
                mods = get_mods(reddit, TARGET_SUBREDDIT)
                last_cleanup_day = current_now.day

            if submission.author and submission.author.name in mods:
                continue

            hashes = get_image_hashes(submission)
            if not hashes: continue

            is_repost = False
            origin_link = None
            for h_str in hashes:
                origin_link = check_for_repost(h_str, cursor)
                if origin_link:
                    is_repost = True
                    break

            if is_repost and submission.subreddit.display_name.lower() == TARGET_SUBREDDIT.lower():
                if submission.permalink != origin_link:
                    submission.reply(f"Detected repost. Original here: https://reddit.com{origin_link}")
                    submission.mod.remove()
                    print(f"REMOVED: {submission.id}")

            if not is_repost:
                for h_str in hashes:
                    cursor.execute("INSERT OR IGNORE INTO posts VALUES (?, ?, ?)", 
                                 (h_str, submission.permalink, submission.created_utc))
                db.commit()

    except Exception as e:
        print(f"Stream Error: {e}. Restarting in 60s...")
        time.sleep(60)
        run_bot()

if __name__ == "__main__":
    run_bot()
