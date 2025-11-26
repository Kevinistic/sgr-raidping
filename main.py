import asyncio
import json
import os
import random
import requests
from dotenv import load_dotenv
from datetime import datetime
from playwright.async_api import async_playwright, Error as PlaywrightError

# === CONFIG ===
load_dotenv()
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
DISCORD_FORUM_URL = "https://discord.com/channels/1000384021542469632/1303601992169426995"
STATE_FILE = "discord_state.json"
THREADS_FILE = "seen_threads.json"
MAX_SEEN_THREADS = 20

if not WEBHOOK_URL:
    raise ValueError("WEBHOOK_URL not found in .env file")

# Headless / UI
HEADLESS = False

ROLE_PING_ID = os.getenv("ROLE_PING_ID", None) # optional

# === STYLE CONFIG ===
FOOTER_TEXT = "brought to you by arle."
FOOTER_ICON = "https://i.imgur.com/JdlwG9w.jpeg"
USERNAME = "Arlecchino"

# Helper: load/save seen threads
def load_seen_threads():
    if os.path.exists(THREADS_FILE):
        try:
            with open(THREADS_FILE, "r") as f:
                return json.load(f)  # list, ordered
        except Exception:
            return []
    return []

def save_seen_threads(seen_list):
    try:
        # truncate to last MAX_SEEN_THREADS
        seen_list[:] = seen_list[-MAX_SEEN_THREADS:]
        with open(THREADS_FILE, "w") as f:
            json.dump(seen_list, f)
    except Exception as e:
        print(f"[Error] Could not write seen threads file: {e}")

# Send webhook (simple blocking requests.post like your other scripts)
def send_payload(payload):
    try:
        response = requests.post(WEBHOOK_URL, json=payload)
        print(f"[Webhook] Status: {response.status_code}")
        return response.status_code
    except Exception as e:
        print(f"[Webhook Error] {e}")
        return None

# Extract thread info from a thread element (best-effort; selectors may need tweaks)
async def extract_thread_data(thread_element):
    try:
        # Title
        title_el = await thread_element.query_selector('[class*="title_f75fb0"], h3, [role="heading"]')
        title = (await title_el.inner_text()) if title_el else "Untitled Thread"

        # Author - try a few possible selectors
        author_el = await thread_element.query_selector('[class*="author"], [class*="username"], span[class*="name"]')
        author = (await author_el.inner_text()) if author_el else "Unknown"

        # Content preview
        message_el = await thread_element.query_selector(
            'div[class*="messageContent_"], div[class*="preview_"], div[class*="markup_"]'
        )
        content = ""
        if message_el:
            content = await message_el.inner_text()

        # Get container with real ID
        container = await thread_element.query_selector('div[data-item-id]')

        thread_id = None
        thread_url = ""

        if container:
            thread_id = await container.get_attribute("data-item-id")
            thread_url = f"{DISCORD_FORUM_URL}/threads/{thread_id}"

        # Creation timestamp: best-effort find time element
        timestamp = None
        time_el = await thread_element.query_selector('time')
        if time_el:
            timestamp = await time_el.get_attribute('datetime')  # ISO if available
        # fallback to now if not available
        if not timestamp:
            timestamp = datetime.utcnow().isoformat() + "Z"

        return {
            "id": thread_id,
            "title": title.strip(),
            "author": author.strip(),
            "content": content.strip(),
            "url": thread_url,
            "timestamp": timestamp
        }
    except Exception as e:
        print(f"[Error extracting thread data] {e}")
        return None

# Build and send webhook for a new thread
def post_new_thread_webhook(thread_data):
    title = thread_data.get("title", "Untitled")
    thread_url = thread_data.get("url", "")
    timestamp = thread_data.get("timestamp", datetime.utcnow().isoformat() + "Z")
    content_preview = thread_data.get("content", "").strip()

    # mention role if ROLE_PING_ID is set
    mention = f"<@&{ROLE_PING_ID}>" if ROLE_PING_ID else ""
    content = mention

    embed = {
        "title": title,

        "description": content_preview if content_preview else "No preview available.",

        "timestamp": None,
        "footer": {
            "text": FOOTER_TEXT,
            "icon_url": FOOTER_ICON
        },
    }

    payload = {
        "username": USERNAME,
        "content": content,
        "embeds": [embed]
    }
    send_payload(payload)
    print(f"[+] Sent webhook for thread: {title} | {thread_url} | {timestamp}")

async def forum_monitor_loop(page, seen_threads):
    """
    Main loop: find thread elements, notify on unseen ones, persist seen IDs.
    This is resilient: continues on DOM errors and sleeps randomly between cycles.
    """
    scroll_counter = 0
    while True:
        try:
            # Wait for some thread-like item to appear
            await page.wait_for_selector('div[role="list"].content_d125d2 li.card_f369db[data-item-role="item"]', timeout=15000)

            thread_elements = await page.query_selector_all(
                'div[role="list"].content_d125d2 li.card_f369db[data-item-role="item"]'
            )

            print(f"[+] Found {len(thread_elements)} thread elements")

            # Process newest-first to keep seen set consistent
            for thread_el in thread_elements:
                thread_data = await extract_thread_data(thread_el)
                if not thread_data:
                    continue
                thread_id = thread_data.get("id")
                # If we couldn't find an ID, use URL+title hash as fallback
                if not thread_id:
                    thread_id = f"fallback:{hash(thread_data.get('url','') + thread_data.get('title',''))}"

                if thread_id == "1303609863024148602": # announcements you dunce
                    print(f"[Blocked Thread Ignored] {thread_data.get('title')} ({thread_id})")
                    seen_threads.append(thread_id)
                    save_seen_threads(seen_threads)
                    continue

                if thread_id not in seen_threads:
                    # New thread detected
                    print(f"[New Thread] {thread_data.get('title')} by {thread_data.get('author')}")
                    # Send webhook for every new thread
                    post_new_thread_webhook(thread_data)
                    # Mark seen and persist
                    seen_threads.append(thread_id)
                    save_seen_threads(seen_threads)

            # Randomize wait to mimic human
            wait_time = random.uniform(5, 12)
            await asyncio.sleep(wait_time)

            # Occasional scroll to keep content fresh
            scroll_counter += 1
            if scroll_counter % random.randint(8, 16) == 0:
                try:
                    await page.mouse.wheel(0, random.randint(-400, 400))
                    print("[+] Scrolled to mimic activity.")
                except PlaywrightError:
                    print("[i] Could not scroll; page or browser closed.")
                    return

            # Occasional reload to catch edge cases
            if scroll_counter % 40 == 0:
                try:
                    print("[+] Refreshing page...")
                    await page.reload()
                    await asyncio.sleep(2)
                except PlaywrightError:
                    print("[i] Page reload failed; page/browser closed.")
                    return

        except PlaywrightError:
            print("[i] Playwright Error (likely page/browser closed). Stopping monitor loop.")
            return
        except Exception as e:
            print(f"[Error] {e}. Retrying in 10 seconds...")
            await asyncio.sleep(10)

async def run():
    print("[+] Starting Forum Thread Monitor (simple webhook pinger)...")
    seen_threads = load_seen_threads()

    async with async_playwright() as p:
        print("[+] Launching Chromium...")
        browser = await p.chromium.launch(headless=HEADLESS, args=["--disable-blink-features=AutomationControlled"])
        context = await browser.new_context(
            viewport={"width": 1280, "height": 720},
            storage_state=STATE_FILE if os.path.exists(STATE_FILE) else None
        )

        # Anti-detection
        await context.add_init_script("""Object.defineProperty(navigator, 'webdriver', { get: () => undefined });""")

        # Manual login if no state
        if not os.path.exists(STATE_FILE):
            print("[+] No saved state found. Please login manually...")
            page = await context.new_page()
            await page.goto("https://discord.com/login")
            print("[!] Login to Discord, then press ENTER to continue.")
            input()
            await context.storage_state(path=STATE_FILE)
            print("[+] Login state saved!")
            await page.close()

        page = await context.new_page()
        await page.goto(DISCORD_FORUM_URL)
        print(f"[+] Opened forum: {DISCORD_FORUM_URL}")

        # Start monitor loop (this will run until page/browser is closed)
        await forum_monitor_loop(page, seen_threads)

        # Clean up
        print("[-] Monitor loop ended, closing browser...")
        await browser.close()

if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("[-] Script stopped by user.")
