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
CLIENT_ID = os.environ.get('REDDIT_CLIENT_ID')
CLIENT_SECRET = os.environ.get('REDDIT_SECRET')
USERNAME = os.environ.get('REDDIT_USERNAME')
PASSWORD = os.environ.get('REDDIT_PASSWORD')
USER_AGENT = "Windows11:AntiMemes Repost Detection:v1.4 (by /u/Curry__Fan)"

TARGET_SUBREDDIT = "AntiMemes"
MONITOR_STR = "AntiMemes+antimeme"
SIMILARITY_THRESHOLD = 3 
DB_PATH = "/data/antimeme_index.db" if os.path.exists("/data") else "antimeme_index.db"
RETENTION_DAYS = 365

# --- NETWORKING & DB ---
session = requests.Session()
retries = Retry(total=5, backoff_factor=1, status_forcelist=[502, 503, 504])
session.mount('https://', HTTPAdapter(max_retries=retries))

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS posts (hash TEXT PRIMARY KEY, link TEXT, timestamp REAL)")
    return conn

def cleanup_old_posts(cursor, db):
    """Deletes entries older than RETENTION_DAYS."""
    cutoff = time.time() - (RETENTION_DAYS * 86400)
    cursor.execute("DELETE FROM posts WHERE timestamp < ?", (cutoff,))
    deleted_count = cursor.rowcount
    db.commit()
    if deleted_count > 0:
        print(f"[{datetime.now()}] Cleanup: Removed {deleted_count} old entries.")

reddit = praw.Reddit(
    client_id=CLIENT_ID, client_secret=CLIENT_SECRET,
    password=PASSWORD, user_agent=USER_AGENT, username=USERNAME
)

# ... (get_image_hashes and check_for_repost functions remain the same) ...

def get_image_hashes(submission):
    urls = []
    if hasattr(submission, 'is_gallery') and submission.is_gallery:
        for item in submission.gallery_data.get('items', []):
            media_id = item['media_id']
            if media_id in submission.media_metadata:
                urls.append(submission.media_metadata[media_id]['s']['u'])
    else:
        urls.append(submission.url)

    hashes = []
    for url in urls:
        if not any(ext in url.lower() for ext in ['.jpg', '.jpeg', '.png', '.webp']):
            continue
        try:
            response = session.get(url, timeout=15)
            img = Image.open(BytesIO(response.content))
            if getattr(img, "is_animated", False):
                img.seek(0)
            hashes.append(str(imagehash.phash(img)))
        except:
            continue
    return hashes

def check_for_repost(curr_hash_str, cursor):
    curr_hash = imagehash.hex_to_hash(curr_hash_str)
    cursor.execute("SELECT hash, link FROM posts")
    for old_hash_str, old_link in cursor.fetchall():
        old_hash = imagehash.hex_to_hash(old_hash_str)
        if (curr_hash - old_hash) <= SIMILARITY_THRESHOLD:
            return old_link
    return None

def get_mods(sub_name):
    try:
        return {mod.name for mod in reddit.subreddit(sub_name).moderator()}
    except:
        return set()

def run_bot():
    db = get_db()
    cursor = db.cursor()
    
    # Track the last day a cleanup was performed
    last_cleanup_day = datetime.now().day
    cleanup_old_posts(cursor, db) # Initial cleanup on start
    
    mods = get_mods(TARGET_SUBREDDIT)
    print(f"Monitoring {MONITOR_STR}...")

    try:
        for submission in reddit.subreddit(MONITOR_STR).stream.submissions(skip_existing=True):
            
            # --- DAILY CLEANUP CHECK ---
            current_now = datetime.now()
            if current_now.day != last_cleanup_day:
                print(f"New day detected ({current_now.date()}). Running scheduled cleanup...")
                cleanup_old_posts(cursor, db)
                last_cleanup_day = current_now.day
                # Refresh mod list once a day too, just in case it changed
                mods = get_mods(TARGET_SUBREDDIT)

            # --- NORMAL BOT LOGIC ---
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
                try:
                    if submission.permalink != origin_link:
                        submission.reply(f"Detected repost. Original here: https://reddit.com{origin_link}")
                        submission.mod.remove()
                        print(f"REMOVED: {submission.id}")
                except Exception as e:
                    print(f"Action Error: {e}")

            if not is_repost:
                for h_str in hashes:
                    cursor.execute("INSERT OR IGNORE INTO posts VALUES (?, ?, ?)", 
                                 (h_str, submission.permalink, submission.created_utc))
                db.commit()
                print(f"INDEXED: {submission.id} from r/{submission.subreddit.display_name}")

    except Exception as e:
        print(f"Stream Error: {e}. Restarting...")
        time.sleep(60)
        run_bot()

if __name__ == "__main__":
    run_bot()
