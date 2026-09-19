# woko-watch

Emails you when a new room listing for Zürich appears on
https://www.woko.ch/unser-angebot/freie-objekte.

## How it works

Each listing on the page links to `detail?oid=NNN`. The script parses every
listing (title, status, availability, address, city, rent), keeps only rooms
in Zürich (drops parking / commercial, drops Winterthur / Dietikon / Dübendorf),
and compares the `oid`s to the ones stored in `seen.json`. Anything unseen
triggers one email (plain text + HTML) via Gmail SMTP, then gets recorded.

## Setup

```bash
cd woko-watch
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # then edit .env
```

### Gmail App Password

1. Turn on 2-Step Verification for the Gmail account you'll send from.
2. Go to https://myaccount.google.com/apppasswords, create one named "woko-watch".
3. Paste the 16-character code into `WOKO_SMTP_PASS` in `.env`.

### First run

```bash
python woko_watch.py --dry-run --all   # see what it parses; nothing is saved/sent
python woko_watch.py --init            # mark current listings as seen
```

`--init` matters: without it the first real run would email you every listing
already on the page.

## Scheduling

**macOS / Linux cron** — every 10 minutes:

```
*/10 * * * * cd /path/to/woko-watch && .venv/bin/python woko_watch.py >> woko.log 2>&1
```

(`crontab -e` to edit. macOS may ask you to grant cron Full Disk Access if the
folder is in Documents/Desktop; keeping it in your home directory avoids that.)

**GitHub Actions** — if you'd rather it run while your laptop is closed: put
this folder in a repo, add the three `WOKO_*` values as repository secrets, and
use a workflow on `schedule: cron: "*/15 * * * *"` that runs the script and
commits `seen.json` back. Happy to write that workflow if you want it.

## Adjusting the filter

- All of Zürich including parking: `WOKO_ROOMS_ONLY=0`
- Every city: `WOKO_ONLY_ZURICH=0`
- To exclude/include other keywords, edit `NON_ROOM_PATTERN` / `ZURICH_PATTERN`
  at the top of the script.

## If it stops working

The parser doesn't depend on CSS class names, only on the `detail?oid=` links
and the German field labels (`Wann`, `Adresse`, `Ort`, `Miete/Monat`). If WOKO
redesigns the page, run `python woko_watch.py --dry-run --all` to see what's
being extracted; a "parsed 0 listings" warning (exit code 2) means the layout
changed enough that the labels need updating.
