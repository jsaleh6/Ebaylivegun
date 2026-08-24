#!/usr/bin/env python3
"""
eBay Auction Sniper
===================

Places a bid on an eBay auction a few seconds before it ends, so competitors
can't react and drive the price up.

DESIGN (this is the important part):
  - You log in MANUALLY once. The script saves your browser session to
    ebay_auth.json. This avoids automating the login form, which is what trips
    eBay's CAPTCHA / 2FA detection and gets accounts flagged.
  - The sniper then reuses that saved session to place the bid at snipe time.

CAVEATS / REALITY CHECK:
  - This automates YOUR OWN account. eBay's User Agreement forbids automated
    access. Worst case = account suspension. Use at your own risk.
  - eBay changes its page structure often. The selectors below are best-effort
    and marked with "# VERIFY". If a run fails, open the bid page in the visible
    browser, inspect the elements, and update the selectors.
  - Don't snipe too tight. Page loads take a few seconds; the default 8-second
    offset is a safe starting point, not 1 second.

SETUP:
    pip install playwright
    playwright install chromium

USAGE:
    # 1) First time only — log in and save your session:
    python ebay_sniper.py login

    # 2) Schedule a snipe:
    python ebay_sniper.py snipe \
        --url "https://www.ebay.com/itm/1234567890" \
        --max-bid 82.50 \
        --end "2026-08-16 19:30:00" \
        --offset 8
"""

import argparse
import asyncio
import sys
from datetime import datetime, timezone

from playwright.async_api import async_playwright

AUTH_FILE = "ebay_auth.json"


# ---------------------------------------------------------------------------
# Mode 1: manual login -> save session
# ---------------------------------------------------------------------------
async def do_login():
    """Open a real browser, let the user log in, then save the session state."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto("https://www.ebay.com/signin/")

        print("\n>> Log in to eBay in the browser window (finish any 2FA).")
        print(">> When you're back on the eBay homepage, press ENTER here.")
        # Block on user input without freezing the event loop:
        await asyncio.get_event_loop().run_in_executor(None, input)

        await context.storage_state(path=AUTH_FILE)
        print(f">> Session saved to {AUTH_FILE}. You're ready to snipe.")
        await browser.close()


# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------
def parse_end_time(end_str: str) -> datetime:
    """Parse 'YYYY-MM-DD HH:MM:SS' as LOCAL time and return an aware datetime."""
    naive = datetime.strptime(end_str, "%Y-%m-%d %H:%M:%S")
    return naive.astimezone()  # attach local tz


async def wait_until(target: datetime):
    """Sleep until `target`, printing a countdown once a second."""
    while True:
        remaining = (target - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            return
        # Print a countdown when we're getting close.
        if remaining <= 60:
            print(f"   snipe in {remaining:5.1f}s", end="\r")
        # Sleep in shrinking steps so the final moment is precise.
        await asyncio.sleep(min(remaining, 0.2 if remaining < 2 else 1.0))


# ---------------------------------------------------------------------------
# Mode 2: place the bid
# ---------------------------------------------------------------------------
async def place_bid(url: str, max_bid: float):
    """Run the eBay bid flow: enter max bid -> place -> confirm.

    Selectors marked '# VERIFY' should be checked against the live page if a
    step fails. The flow is intentionally verbose so you can see where it stops.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(storage_state=AUTH_FILE)
        page = await context.new_page()

        print(">> Loading item page...")
        await page.goto(url, wait_until="domcontentloaded")

        # Step 1: click the "Place bid" button on the item page.  # VERIFY
        try:
            await page.get_by_role("button", name="Place bid").click(timeout=8000)
        except Exception:
            # Fallback: some layouts use a link or different label.
            await page.get_by_text("Place bid", exact=False).first.click(timeout=8000)

        # Step 2: type the max bid into the amount field.  # VERIFY
        # eBay's field is often an <input> for the bid amount.
        bid_input = page.locator("input[name='maxbid'], input#binBid, input[type='text']").first
        await bid_input.fill(str(max_bid), timeout=8000)

        # Step 3: submit the bid.  # VERIFY
        await page.get_by_role("button", name="Place bid").last.click(timeout=8000)

        # Step 4: confirm on the review screen.  # VERIFY
        # eBay usually shows a "Confirm bid" step. If it's absent, this is skipped.
        try:
            await page.get_by_role("button", name="Confirm bid").click(timeout=6000)
        except Exception:
            print("   (no confirm step found — bid may already be placed)")

        # Give the result page a moment, then report what we see.
        await page.wait_for_timeout(2500)
        body = (await page.inner_text("body")).lower()
        if "you're the highest bidder" in body or "highest bidder" in body:
            print(">> RESULT: You are the highest bidder.")
        elif "you've been outbid" in body or "outbid" in body:
            print(">> RESULT: Outbid — someone's max was higher.")
        else:
            print(">> RESULT: Unclear. Check the browser window / your eBay account.")

        input(">> Press ENTER to close the browser...")
        await browser.close()


async def do_snipe(url: str, max_bid: float, end_str: str, offset: int):
    end_time = parse_end_time(end_str)
    fire_time = end_time.astimezone(timezone.utc)
    fire_time = fire_time.timestamp() - offset
    fire_dt = datetime.fromtimestamp(fire_time, tz=timezone.utc)

    print(f">> Auction ends:  {end_time}")
    print(f">> Firing bid at: {fire_dt.astimezone()}  ({offset}s before end)")
    print(f">> Max bid:       {max_bid}")

    await wait_until(fire_dt)
    print("\n>> FIRING NOW")
    await place_bid(url, max_bid)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="eBay auction sniper")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("login", help="Log in manually and save your session")

    s = sub.add_parser("snipe", help="Schedule a snipe bid")
    s.add_argument("--url", required=True, help="Full eBay item URL")
    s.add_argument("--max-bid", required=True, type=float, help="Your maximum bid")
    s.add_argument("--end", required=True,
                   help="Auction end time, local, 'YYYY-MM-DD HH:MM:SS'")
    s.add_argument("--offset", type=int, default=8,
                   help="Seconds before end to fire the bid (default 8)")

    args = parser.parse_args()

    if args.cmd == "login":
        asyncio.run(do_login())
    elif args.cmd == "snipe":
        asyncio.run(do_snipe(args.url, args.max_bid, args.end, args.offset))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(1)
