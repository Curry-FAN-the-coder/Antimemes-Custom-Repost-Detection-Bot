import praw
import sqlite3
import requests
import imagehash
import time
from PIL import Image
from io import BytesIO

# --- CONFIGURATION ---
# Create an app at https://reddit.com (choose "script")
CLIENT_ID = "YOUR_CLIENT_ID"
CLIENT_SECRET = "YOUR_CLIENT_SECRET"
PASSWORD = "YOUR_PASSWORD"
USERNAME = "YOUR_BOT_USERNAME"
USER_AGENT = "AntiMemeSentinel_v1.0_by_u/YourUsername"

TARGET_SUBREDDIT = "AntiMemes"
SCAN_SUBREDDITS = ["AntiMemes", "antimeme"]
SIMILARITY_THRESHOLD = 3  # Hamming distance. 3 is roughly a 95% match.
DAYS_TO_BACKFILL = 365

# --- INITIALIZATION ---
reddit = praw.Reddit(
    client_id=CLIENT_ID,
    client_secret=CLIENT_SECRET,
    password=PASSWORD,
    user_agent=USER_AGENT,
    username=USERNAME
)

# Connect to SQLite database
db = sqlite3.connect("antimeme_index.db")
cursor = db.cursor()
cursor.execute("CREATE TABLE IF NOT EXISTS posts (hash TEXT PRIMARY KEY, link TEXT, timestamp REAL)")
db.commit()

def get_image_hash(url):
    """Downloads an image and returns its perceptual hash string."""
    if not any(url.lower().endswith(ext) for ext in ['.jpg', '.jpeg', '.png']):
        return None
    try:
        response = requests.get(url, timeout=10)
        img = Image.open(BytesIO(response.content))
        return str(imagehash.phash(img))
    except Exception:
        return None

def run_backfill():
    """Fetches historical posts from the last year to populate the database."""
    print(f"--- Starting backfill for the last {DAYS_TO_BACKFILL} days ---")
    start_time = int(time.time()) - (DAYS_TO_BACKFILL * 86400)
    
    for sub in SCAN_SUBREDDITS:
        print(f"Scanning history of r/{sub}...")
        # PullPush API allows fetching beyond the standard 1,000 post limit
        url = f"https://pullpush.io{sub}&after={start_time}&size=100"
        
        while True:
            try:
                response = requests.get(url, timeout=15).json()
                data = response.get('data', [])
                if not data:
                    break
                
                for post in data:
                    post_url = post.get('url', '')
                    if post_url:
                        h = get_image_hash(post_url)
                        if h:
                            # Use IGNORE to avoid errors on duplicate hashes found in history
                            cursor.execute("INSERT OR IGNORE INTO posts VALUES (?, ?, ?)", 
                                           (h, post['permalink'], post['created_utc']))
                
                # Move to next batch of results
                last_time = data[-1]['created_utc']
                url = f"https://pullpush.io{sub}&after={last_time}&size=100"
                db.commit()
                print(f"Indexed up to {time.ctime(last_time)}")
                time.sleep(1) # Rate limit protection
            except Exception as e:
                print(f"Backfill error: {e}")
                break
    print("--- Backfill complete. Database is primed. ---")

def run_bot():
    # 1. Automatic Onboarding Check
    cursor.execute("SELECT COUNT(*) FROM posts")
    if cursor.fetchone()[0] == 0:
        run_backfill()

    # 2. Live Stream Monitoring
    print(f"--- Live: Monitoring r/{TARGET_SUBREDDIT} ---")
    subreddit = reddit.subreddit(TARGET_SUBREDDIT)
    
    for submission in subreddit.stream.submissions(skip_existing=True):
        curr_hash_str = get_image_hash(submission.url)
        if not curr_hash_str:
            continue

        curr_hash = imagehash.hex_to_hash(curr_hash_str)
        
        # Check against local database
        cursor.execute("SELECT hash, link FROM posts")
        repost_link = None
        exact_match = False

        for old_hash_str, old_link in cursor.fetchall():
            old_hash = imagehash.hex_to_hash(old_hash_str)
            diff = curr_hash - old_hash # Difference between fingerprints
            
            if diff <= SIMILARITY_THRESHOLD:
                repost_link = old_link
                if diff == 0:
                    exact_match = True
                break

        # 3. Action: Remove and Comment
        if repost_link:
            full_link = f"https://reddit.com{repost_link}"
            submission.reply(f"Sorry, you've already posted this (Likely Repost: {full_link})")
            submission.mod.remove()
            print(f"[ACTION] Removed {submission.id} - Match found: {repost_link}")
        
        # 4. Indexing: Save new unique content (if not a 100% match)
        if not exact_match:
            cursor.execute("INSERT OR REPLACE INTO posts VALUES (?, ?, ?)", 
                           (curr_hash_str, submission.permalink, submission.created_utc))
            db.commit()

if __name__ == "__main__":
    try:
        run_bot()
    except KeyboardInterrupt:
        print("Bot stopped.")
    finally:
        db.close()
