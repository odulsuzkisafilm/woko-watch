#!/usr/bin/env python3
"""
woko_watch.py — watch https://www.woko.ch/unser-angebot/freie-objekte for new
room listings in Zürich and email you when one appears.

Usage:
    python woko_watch.py            # check once, email if new listings
    python woko_watch.py --dry-run  # check once, print instead of emailing
    python woko_watch.py --init     # mark everything currently listed as seen
                                    # (run this first so you don't get 10 emails)
    python woko_watch.py --html file.html   # parse a saved page (for testing)

Configuration is via environment variables (or a .env file next to the script):
    WOKO_SMTP_USER      your Gmail address (sender)
    WOKO_SMTP_PASS      a Gmail *App Password* (not your normal password)
    WOKO_EMAIL_TO       where to send alerts (defaults to WOKO_SMTP_USER)
    WOKO_STATE_FILE     path to JSON of seen listing ids (default: seen.json)
    WOKO_ONLY_ZURICH    "1" (default) to keep only Zürich, "0" for all cities
    WOKO_ROOMS_ONLY     "1" (default) to drop parking/commercial, "0" for all
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import smtplib
import sys
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

import requests
from bs4 import BeautifulSoup, Tag

URL = "https://www.woko.ch/unser-angebot/freie-objekte"
BASE = "https://www.woko.ch"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
    ),
    "Accept-Language": "de-CH,de;q=0.9,en;q=0.8",
}

# Titles containing any of these are NOT rooms (parking, commercial, storage).
NON_ROOM_PATTERN = re.compile(
    r"parkplatz|parkplätze|parkplaetze|garage|töff|toeff|motorrad|"
    r"gewerbe|büro|buero|lager|keller|atelier|parking",
    re.IGNORECASE,
)
ZURICH_PATTERN = re.compile(r"\b80\d\d\b|z[üu]rich", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def load_dotenv(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE lines) so no extra dependency is needed."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def env_flag(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


# --------------------------------------------------------------------------- #
# Scraping
# --------------------------------------------------------------------------- #
@dataclass
class Listing:
    oid: str
    title: str
    status: str
    available: str
    address: str
    city: str
    rent: str
    url: str

    def is_room(self) -> bool:
        return not NON_ROOM_PATTERN.search(self.title)

    def in_zurich(self) -> bool:
        return bool(ZURICH_PATTERN.search(self.city or ""))

    def fingerprint(self) -> str:
        """Content hash. WOKO reuses the same oid when a room is re-listed,
        so an oid alone is not enough to tell 'new' from 'seen'."""
        raw = "|".join(
            re.sub(r"\s+", " ", x).strip().lower()
            for x in (self.title, self.status, self.available, self.address, self.city, self.rent)
        )
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]

    def pretty(self) -> str:
        return (
            f"{self.title}\n"
            f"  {self.status}\n"
            f"  Ab:      {self.available}\n"
            f"  Adresse: {self.address}\n"
            f"  Ort:     {self.city}\n"
            f"  Miete:   {self.rent}\n"
            f"  {self.url}"
        )


def fetch_html() -> str:
    resp = requests.get(URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


def _field(text: str, label: str) -> str:
    """Grab the value after e.g. 'Ort:' up to the next known label or newline."""
    m = re.search(
        rf"{label}\s*:?\s*(.+?)(?=\s*(?:Wann|Adresse|Ort|Miete/Monat|Detail|Bewerben)\b|$)",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""


def _card_for(anchor: Tag) -> Tag:
    """Walk up from a 'Detail' link until we find the block holding the whole
    listing (it must contain both the rent and the city label)."""
    node: Tag = anchor
    for _ in range(12):
        parent = node.parent
        if parent is None or not isinstance(parent, Tag) or parent.name in ("body", "html", "[document]"):
            break
        oids = {
            m.group(1)
            for l in parent.select('a[href*="detail?oid="]')
            for m in [re.search(r"oid=(\d+)", l.get("href", ""))]
            if m
        }
        if len(oids) > 1:
            # parent spans several listings -> the current node is the card
            return node
        node = parent
        txt = node.get_text(" ", strip=True)
        if "Miete" in txt and re.search(r"\bOrt\b", txt):
            return node
    return node


def parse_listings(html: str) -> list[Listing]:
    soup = BeautifulSoup(html, "html.parser")
    seen: dict[str, Listing] = {}

    for a in soup.select('a[href*="detail?oid="]'):
        m = re.search(r"oid=(\d+)", a.get("href", ""))
        if not m:
            continue
        oid = m.group(1)
        if oid in seen:
            continue

        card = _card_for(a)
        text = card.get_text("\n", strip=True)
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        flat = " ".join(lines)

        # Title = first line that isn't a label/status/button.
        title = ""
        for ln in lines:
            if re.match(r"^(Wann|Adresse|Ort|Miete|Detail|Bewerben)\b", ln, re.I):
                continue
            if re.search(r"gesucht", ln, re.I):
                continue
            title = ln
            break

        status_m = re.search(r"(Unter|Nach)mieter gesucht", flat, re.I)
        href = a["href"]
        url = href if href.startswith("http") else BASE + href

        seen[oid] = Listing(
            oid=oid,
            title=title or f"Objekt {oid}",
            status=status_m.group(0) if status_m else "",
            available=_field(flat, "Wann").removeprefix("Ab").strip(),
            address=_field(flat, "Adresse"),
            city=_field(flat, "Ort"),
            rent=_field(flat, "Miete/Monat"),
            url=url,
        )

    return list(seen.values())


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
def load_seen(path: Path) -> dict[str, str]:
    """Returns {oid: fingerprint}. Accepts the old format (a plain list of
    oids) by mapping each to an empty fingerprint, so the first run after
    upgrading treats every unchanged listing as already seen."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8")).get("seen", {})
    except (json.JSONDecodeError, AttributeError):
        return {}
    if isinstance(data, list):
        return {str(oid): "" for oid in data}
    return {str(k): str(v) for k, v in data.items()}


def save_seen(path: Path, seen: dict[str, str]) -> None:
    ordered = {k: seen[k] for k in sorted(seen, key=int)}
    path.write_text(
        json.dumps(
            {"seen": ordered, "updated": datetime.now().isoformat(timespec="seconds")},
            indent=2,
        ),
        encoding="utf-8",
    )


# --------------------------------------------------------------------------- #
# Email
# --------------------------------------------------------------------------- #
def send_email(new: list[Listing]) -> None:
    user = os.environ.get("WOKO_SMTP_USER")
    pw = os.environ.get("WOKO_SMTP_PASS")
    to = os.environ.get("WOKO_EMAIL_TO", user)
    if not (user and pw and to):
        sys.exit(
            "Missing email config: set WOKO_SMTP_USER, WOKO_SMTP_PASS "
            "(Gmail App Password) and optionally WOKO_EMAIL_TO."
        )

    n = len(new)
    subject = f"[WOKO] {n} new/updated room{'s' if n != 1 else ''} in Zürich: " + \
        "; ".join(f"{l.rent} {l.address}" for l in new[:3])

    body = "\n\n".join(l.pretty() for l in new) + f"\n\nAll listings: {URL}\n"

    html_items = "".join(
        f"<li style='margin-bottom:14px'><b>{l.title}</b> <i>({l.status})</i><br>"
        f"Ab {l.available} · {l.address}, {l.city} · <b>{l.rent}</b><br>"
        f"<a href='{l.url}'>Detail</a></li>"
        for l in new
    )
    html = f"<html><body><ul>{html_items}</ul><p><a href='{URL}'>All listings</a></p></body></html>"

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    msg.set_content(body)
    msg.add_alternative(html, subtype="html")

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(user, pw)
        s.send_message(msg)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    here = Path(__file__).resolve().parent
    load_dotenv(here / ".env")

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="print new listings instead of emailing")
    ap.add_argument("--init", action="store_true", help="mark all current listings as seen, send nothing")
    ap.add_argument("--html", type=Path, help="parse this saved HTML file instead of fetching")
    ap.add_argument("--all", action="store_true", help="print all parsed listings (debug)")
    args = ap.parse_args()

    state_file = Path(os.environ.get("WOKO_STATE_FILE", here / "seen.json"))
    only_zurich = env_flag("WOKO_ONLY_ZURICH", True)
    rooms_only = env_flag("WOKO_ROOMS_ONLY", True)

    html = args.html.read_text(encoding="utf-8") if args.html else fetch_html()
    listings = parse_listings(html)
    if not listings:
        print("WARNING: parsed 0 listings — the page layout may have changed.", file=sys.stderr)
        return 2

    if args.all:
        for l in listings:
            print(l.pretty(), "\n")

    wanted = [
        l for l in listings
        if (not rooms_only or l.is_room()) and (not only_zurich or l.in_zurich())
    ]

    seen = load_seen(state_file)
    current = {l.oid: l.fingerprint() for l in listings}

    # New = never seen this oid. Updated = same oid, but the content changed
    # (WOKO re-lists rooms under the same oid). An empty stored fingerprint
    # means "known from the old list-format state": accept silently.
    new = [l for l in wanted if l.oid not in seen]
    updated = [
        l for l in wanted
        if l.oid in seen and seen[l.oid] and seen[l.oid] != l.fingerprint()
    ]
    alerts = new + updated

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    if args.init:
        save_seen(state_file, {**seen, **current})
        print(f"[{stamp}] Initialised: {len(listings)} listings marked as seen ({len(wanted)} match your filter).")
        return 0

    if not alerts:
        print(f"[{stamp}] {len(listings)} listings, {len(wanted)} match filter, nothing new.")
    else:
        print(f"[{stamp}] {len(new)} NEW, {len(updated)} UPDATED listing(s):")
        for l in alerts:
            tag = "NEW" if l in new else "UPDATED"
            print(f"[{tag}]", l.pretty(), "\n")
        if not args.dry_run:
            send_email(alerts)
            print("Email sent.")

    # Remember every listing we've observed (not just matches) so a later
    # filter change doesn't re-alert on old listings. Dry runs leave state
    # untouched so you can re-run them while testing.
    if not args.dry_run:
        save_seen(state_file, {**seen, **current})
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except requests.RequestException as e:
        print(f"Network error: {e}", file=sys.stderr)
        sys.exit(1)
