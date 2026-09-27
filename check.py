#!/usr/bin/env python3
"""
One-shot Weeztix ticket check, designed to run on a schedule (GitHub Actions).

Each run: load the shop page, check each ticket, send an ntfy push if
something is purchasable, then exit. A small state.json file remembers the
ticket list between runs so changes (new ticket types, prices) can be flagged.

Environment variables:
    NTFY_TOPIC   your ntfy topic (set as a GitHub secret)
    TEST_NOTIFY  set to "1" to just send a test push and exit
"""

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

URL = (
    "https://shop.weeztix.com/18ff0d60-decc-4846-b1d4-51d3c5b91efc/tickets"
    "?shop_code=zke7c2d4"
)
TICKETS = [
    "Regional Player Ticket",
    "Treasure Cup Player Ticket",
]
SOLD_OUT_TEXT = "sold out"
STATE_FILE = Path("state.json")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()


def log(msg):
    print(f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC] {msg}", flush=True)


def notify(title, message):
    log(f"*** {title}: {message}")
    if not NTFY_TOPIC:
        log("NTFY_TOPIC not set - no push sent.")
        return
    try:
        r = requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode(),
            headers={"Title": title, "Priority": "urgent", "Click": URL,
                     "Tags": "ticket"},
            timeout=15,
        )
        log(f"ntfy response: {r.status_code}")
    except Exception as e:
        log(f"ntfy failed: {e}")


def ticket_status(text):
    """{name: 'sold_out' | 'available' | 'missing'} based on each ticket's row."""
    lower = text.lower()
    found = sorted((lower.find(n.lower()), n) for n in TICKETS
                   if lower.find(n.lower()) != -1)
    status = {n: "missing" for n in TICKETS}
    for k, (start, name) in enumerate(found):
        end = found[k + 1][0] if k + 1 < len(found) else len(lower)
        status[name] = "sold_out" if SOLD_OUT_TEXT in lower[start:end] else "available"
    return status, (found[0][0] if found else None)


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")


def fetch():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"))
        page.goto(URL, wait_until="networkidle", timeout=60_000)
        try:
            page.get_by_text(TICKETS[0]).first.wait_for(timeout=20_000)
        except PWTimeout:
            pass
        page.wait_for_timeout(1500)
        text, final_url = page.inner_text("body"), page.url
        browser.close()
        return text, final_url


def main():
    if os.environ.get("TEST_NOTIFY") == "1":
        notify("Ticket watcher test", "GitHub Actions can reach your phone!")
        return

    state = load_state()
    text, final_url = fetch()

    if "queue-it" in final_url:
        notify("Queue is open!", "The shop redirected to a waiting queue - go now!")
        return

    status, start = ticket_status(text)
    log(", ".join(f"{n}: {s}" for n, s in status.items()))

    if start is None:
        log("No ticket names found - page didn't load or was blocked. "
            "Page text starts with:\n" + text[:500])
        return

    available = [n for n, s in status.items() if s == "available"]
    if available:
        # Repeats every run while available, so you can't miss it
        notify("Tickets available!", f"{' & '.join(available)} can be bought now!")

    h = hashlib.sha256(text[start:].encode()).hexdigest()
    if state.get("hash") and state["hash"] != h and not available:
        notify("Ticket list changed",
               "Still sold out, but something changed - take a look.")

    save_state({"hash": h, "status": status})


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"Error: {e}")
        sys.exit(0)   # don't spam GitHub failure emails on a flaky load
