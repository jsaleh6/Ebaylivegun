#!/usr/bin/env python3
"""
eBay Live Auto-Rebidder
=======================

eBay Live has no "set your max and walk away" proxy bidding — you tap "Bid" for
every increment. This bot does that for you: it watches the live bidding widget
and, the instant you're outbid, places the next increment automatically, up to a
ceiling you set.

WHY NOT A SNIPER:  eBay Live uses a soft-close (a late bid extends the clock), so
speed alone wins nothing. The edge here is never-miss-an-increment auto-rebidding,
not last-second timing.

REALITY CHECK:
  - The Live bidding widget only exists during a live event, behind login, as a
    real-time video overlay. Its HTML is NOT publicly documented and WILL differ
    from the guesses below. That's why `discover` mode exists — run it during a
    real event to find the actual selectors, then paste them into the SELECTORS
    block. Expect to do this once per major eBay UI change.
  - Automates your own account; eBay's User Agreement forbids automated access.
    Account-suspension risk is on you. Use only for items you actually want.
  - Reuses the ebay_auth.json session from the sniper script. Run that script's
    `login` step first if you don't have it yet.

SETUP:
    pip install playwright
    playwright install chromium

USAGE:
    # 1) During a live event, find the real selectors:
    python ebay_live_bidder.py discover --url "https://www.ebay.com/eBayLive/..."

    # 2) Paste findings into SELECTORS below, then run the bidder:
    python ebay_live_bidder.py bid \
        --url "https://www.ebay.com/eBayLive/..." \
        --max 120 \
        --item "Charizard"      # optional keyword to only bid on a matching lot
"""

import argparse
import asyncio
import re
import sys
import threading

from playwright.async_api import async_playwright

AUTH_FILE = "ebay_auth.json"      # created by the sniper script's `login` step
POLL_SECONDS = 0.4                # how often to re-read the widget

# ---------------------------------------------------------------------------
# SELECTORS  --  the part you MUST verify with `discover` mode.
# These are educated guesses. Replace them with what you find live.
# ---------------------------------------------------------------------------
SELECTORS = {
    # The button you tap to place a bid. Often shows the next amount, e.g. "Bid $45".
    "bid_button": "button:has-text('Bid')",
    # Text shown when you currently hold the high bid.
    "is_highest_text": "text=/highest bidder/i",
    # Text shown when you've been outbid.
    "outbid_text": "text=/outbid/i",
    # Element showing the current lot's title (for the --item keyword filter).
    "item_title": "[data-testid='live-item-title'], .live-item-title",
    # Text that means the current lot has closed.
    "sold_text": "text=/\\bsold\\b/i",
}

MONEY_RE = re.compile(r"\$?\s*([\d,]+(?:\.\d{2})?)")


def parse_money(text: str):
    """Pull the first dollar amount out of a string, or return None."""
    if not text:
        return None
    m = MONEY_RE.search(text.replace(",", ""))
    return float(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# discover mode: dump the widget so you can identify real selectors
# ---------------------------------------------------------------------------
async def do_discover(url: str):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(storage_state=AUTH_FILE)
        page = await context.new_page()
        await page.goto(url, wait_until="domcontentloaded")

        print("\n>> Get to a live lot with an active 'Bid' button, then press ENTER.")
        await asyncio.get_event_loop().run_in_executor(None, input)

        # Recursive walker that DOES descend into open shadow roots (web components).
        # The old query_selector_all only saw the light DOM, so a Bid button tucked
        # inside a component was invisible. This opens those compartments too.
        # NOTE: it cannot see into *closed* shadow roots — nothing in the browser can.
        # If this still finds no Bid button, that's the genuine wall.
        js = r"""
        () => {
          const out = [];
          const seen = new Set();
          const rx = /\$|\bbid\b|outbid|highest/i;
          function walk(root){
            let els;
            try { els = root.querySelectorAll('*'); } catch(e){ return; }
            for (const el of els){
              if (el.shadowRoot) walk(el.shadowRoot);   // <-- descend into components
              let txt = '';
              try { txt = (el.textContent || '').trim(); } catch(e){}
              const clickable = el.tagName === 'BUTTON'
                || el.getAttribute('role') === 'button'
                || (el.className && (''+el.className).toLowerCase().includes('bid'));
              if (!txt && !clickable) continue;
              if (txt.length > 80) continue;
              if (!rx.test(txt) && !clickable) continue;
              const key = el.tagName + '|' + txt + '|' + clickable;
              if (seen.has(key)) continue;
              seen.add(key);
              out.push({
                tag: el.tagName.toLowerCase(),
                testid: el.getAttribute('data-testid') || '',
                cls: ((el.className && (''+el.className)) || '').slice(0,50),
                text: txt.slice(0,80),
                clickable: clickable
              });
            }
          }
          walk(document);
          return out;
        }
        """
        found = await page.evaluate(js)

        print("\n===== CANDIDATES (now includes shadow-DOM components) =====")
        clickers = [f for f in found if f["clickable"]]
        if clickers:
            print("\n-- Likely BID BUTTONS (clickable): --")
            for f in clickers:
                print(f"  <{f['tag']}> testid='{f['testid']}' class='{f['cls']}'  ->  {f['text']!r}")
        print("\n-- Other price / status text: --")
        for f in found:
            if f["clickable"]:
                continue
            print(f"  <{f['tag']}> testid='{f['testid']}' class='{f['cls']}'  ->  {f['text']!r}")

        if not found:
            print("  (nothing found — either not inside a live item yet, or the widget "
                  "uses a CLOSED shadow root, which no browser tool can read.)")

        print("\n>> Use the tag / testid / class above to fill in SELECTORS.")
        input(">> Press ENTER to close.")
        await browser.close()


# ---------------------------------------------------------------------------
# bid mode: monitor + auto-rebid to ceiling
# ---------------------------------------------------------------------------
async def read_state(page, item_keyword):
    """Return current widget state as a dict."""
    async def visible(sel):
        try:
            el = page.locator(sel).first
            return await el.is_visible(timeout=200)
        except Exception:
            return False

    async def text_of(sel):
        try:
            return (await page.locator(sel).first.inner_text(timeout=200)).strip()
        except Exception:
            return ""

    title = await text_of(SELECTORS["item_title"])
    bid_label = await text_of(SELECTORS["bid_button"])

    return {
        "title": title,
        "is_highest": await visible(SELECTORS["is_highest_text"]),
        "outbid": await visible(SELECTORS["outbid_text"]),
        "sold": await visible(SELECTORS["sold_text"]),
        "next_bid": parse_money(bid_label),   # amount shown on the Bid button
        "matches_item": (item_keyword.lower() in title.lower()) if item_keyword else True,
    }


# ---------------------------------------------------------------------------
# ON / OFF switch
# A tiny background thread reads the keyboard. Pressing ENTER pauses or resumes
# the bot; typing 'q' + ENTER quits. This lets you flip it off the moment you
# don't want it bidding — e.g. a lot you'd rather bid on by hand.
# ---------------------------------------------------------------------------
CONTROL = {"paused": False, "quit": False}


def _key_listener():
    while True:
        line = sys.stdin.readline()
        if line.strip().lower() == "q":
            CONTROL["quit"] = True
            return
        CONTROL["paused"] = not CONTROL["paused"]
        print("\n>> " + ("PAUSED — press ENTER to resume."
                         if CONTROL["paused"] else "RESUMED — bidding is ON."))


async def do_bid(url: str, max_bid: float, item_keyword: str, open_below):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(storage_state=AUTH_FILE)
        page = await context.new_page()
        await page.goto(url, wait_until="domcontentloaded")

        print(f">> Watching. Ceiling: ${max_bid}"
              + (f" | entry gate: only lots opening at/below ${open_below}"
                 if open_below is not None else "")
              + (f" | only lot matching '{item_keyword}'" if item_keyword else ""))
        print(">> ON/OFF: press ENTER to pause or resume.  Type 'q' + ENTER to quit.\n")

        # Start the keyboard on/off listener in the background.
        threading.Thread(target=_key_listener, daemon=True).start()

        last_action = 0.0
        engaged = set()   # lot titles we've committed to compete for (penny-hunt gate)
        while True:
            if CONTROL["quit"]:
                print("\n>> Quitting.")
                break
            if CONTROL["paused"]:
                await asyncio.sleep(0.3)      # idle while switched off
                continue

            s = await read_state(page, item_keyword)

            if s["sold"]:
                print(f"   lot closed: {s['title'][:40]}")
                await asyncio.sleep(1.5)      # wait for next lot to load
                continue

            if not s["matches_item"]:
                await asyncio.sleep(POLL_SECONDS)
                continue

            status = ("HIGH" if s["is_highest"] else
                      "OUTBID" if s["outbid"] else "—")
            nb = f"${s['next_bid']}" if s["next_bid"] else "?"
            print(f"   {s['title'][:30]:30} status={status:6} next={nb}", end="\r")

            # Entry gate (penny hunting): if --open-below is set, only compete for
            # lots we first saw priced at/below it. A lot that opens above the gate
            # is ignored entirely, even if it later dips.
            lot = s["title"]
            if open_below is None:
                active = True
            else:
                if (lot and lot not in engaged and not s["is_highest"]
                        and s["next_bid"] is not None and s["next_bid"] <= open_below):
                    engaged.add(lot)
                    print(f"\n   >> engaging cheap lot: {lot[:40]} (@ ${s['next_bid']})")
                active = lot in engaged

            # Decide whether to fire a bid.
            need_to_bid = active and (s["outbid"] or (not s["is_highest"] and s["next_bid"]))
            within_max = s["next_bid"] is not None and s["next_bid"] <= max_bid
            # debounce: don't spam clicks faster than the UI can update
            cooled_down = (asyncio.get_event_loop().time() - last_action) > 1.5

            if need_to_bid and within_max and cooled_down:
                try:
                    await page.locator(SELECTORS["bid_button"]).first.click(timeout=1500)
                    last_action = asyncio.get_event_loop().time()
                    print(f"\n   >> BID placed at ${s['next_bid']}")
                    # eBay Live may pop a confirm — try it, ignore if absent.
                    try:
                        await page.get_by_role("button", name=re.compile("confirm", re.I)
                                               ).click(timeout=1000)
                    except Exception:
                        pass
                except Exception as e:
                    print(f"\n   (bid click failed: {e})")

            elif need_to_bid and not within_max:
                print(f"\n   ceiling hit: next bid ${s['next_bid']} > max ${max_bid}. "
                      "Holding.")

            await asyncio.sleep(POLL_SECONDS)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="eBay Live auto-rebidder")
    sub = parser.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("discover", help="Dump widget elements to find selectors")
    d.add_argument("--url", required=True, help="eBay Live event URL")

    b = sub.add_parser("bid", help="Auto-rebid to a ceiling")
    b.add_argument("--url", required=True, help="eBay Live event URL")
    b.add_argument("--max", required=True, type=float, help="Your maximum bid")
    b.add_argument("--item", default="", help="Only bid on lots whose title contains this")
    b.add_argument("--open-below", type=float, default=None,
                   help="Penny-hunt gate: only compete for lots that OPEN at/below "
                        "this price (e.g. 0.50). Lots that start pricier are skipped.")

    args = parser.parse_args()
    if args.cmd == "discover":
        asyncio.run(do_discover(args.url))
    elif args.cmd == "bid":
        asyncio.run(do_bid(args.url, args.max, args.item, args.open_below))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(0)
