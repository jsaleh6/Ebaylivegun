# Ebaylivegun

Two Playwright-based scripts for automating bids on your own eBay account.

- **`ebay_sniper.py`** — logs in once, saves the session, then places a single
  bid a few seconds before an auction ends.
- **`ebay_live_bidder.py`** — watches an eBay Live bidding widget and
  auto-rebids the next increment whenever you're outbid, up to a ceiling you
  set.

## Setup

```bash
pip install -r requirements.txt
playwright install chromium
```

## Usage

Log in once (shared by both scripts):

```bash
python ebay_sniper.py login
```

Schedule a snipe:

```bash
python ebay_sniper.py snipe \
    --url "https://www.ebay.com/itm/1234567890" \
    --max-bid 82.50 \
    --end "2026-08-16 19:30:00" \
    --offset 8
```

Auto-rebid a live auction:

```bash
python ebay_live_bidder.py bid \
    --url "https://www.ebay.com/eBayLive/..." \
    --max 120 \
    --item "Charizard"
```

## Notes

- Both scripts automate your own eBay account. eBay's User Agreement forbids
  automated access — worst case is account suspension. Use at your own risk,
  and only on items you actually want.
- `ebay_auth.json` holds your live login session and is gitignored — never
  commit it.
