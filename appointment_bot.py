#!/usr/bin/env python3
"""
Heilbronn Ausländerbehörde appointment bot (eTermin).

Watches https://www.etermin.net/qtermin-stadtheilbronn-abh for a free slot
earlier than a cutoff date and books it automatically with your details.

  python appointment_bot.py            # loop forever (run on your laptop)
  python appointment_bot.py --once     # single check (for cron / GitHub Actions)
  python appointment_bot.py --once --show   # single check with a visible browser

Exit codes with --once:  0 = nothing found, 10 = BOOKED, 2 = error.

Safety:
  * dry_run = true (default) goes all the way to the filled-in form and stops
    before "Termin buchen". Set dry_run = false only after a dry run looked right.
  * After one successful booking it writes booked.json and never books again.
  * Re-checks the chosen date on the summary before clicking "Termin buchen".
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import random
import re
import sys
import time
import tomllib
import traceback
import urllib.parse
import urllib.request
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

URL = "https://www.etermin.net/qtermin-stadtheilbronn-abh"
HERE = Path(__file__).resolve().parent
LOCK_FILE = HERE / "booked.json"
SHOT_DIR = HERE / "screenshots"
TIME_SLOT_RE = re.compile(r"^\s*\d{2}:\d{2}\s*-\s*\d{2}:\d{2}\s*$")
SUMMARY_DATE_RE = re.compile(r"(\d{2}\.\d{2}\.\d{4})\s+(\d{2}:\d{2})\s*-\s*\d{2}:\d{2}")


# ----------------------------------------------------------------- utilities
def log(msg: str) -> None:
    print(f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def load_config(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"Config not found: {path}\nCopy config.example.toml to config.toml and fill it in.")
    with path.open("rb") as f:
        cfg = tomllib.load(f)
    me = cfg["person"]
    for key in ("salutation", "first_name", "last_name", "birthday", "city", "phone", "email"):
        if not str(me.get(key, "")).strip():
            sys.exit(f"config.toml: [person] {key} is required by the booking form.")
    dt.date.fromisoformat(me["birthday"])  # validates YYYY-MM-DD
    dt.date.fromisoformat(cfg["search"]["book_before"])
    return cfg


def notify(cfg: dict, title: str, message: str, priority: str = "default") -> None:
    """Push to your phone via ntfy.sh (free app, no account) + terminal bell."""
    print("\a", end="", flush=True)
    log(f"NOTIFY: {title} | {message}")
    topic = cfg.get("notify", {}).get("ntfy_topic", "").strip()
    if not topic:
        return
    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{urllib.parse.quote(topic)}",
            data=message.encode("utf-8"),
            headers={
                "Title": title.encode("utf-8").decode("latin-1", "ignore"),
                "Priority": priority,
                "Click": URL,
                "Tags": "calendar",
            },
            method="POST",
        )
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:  # never let a notification failure stop the bot
        log(f"ntfy failed: {e}")


def shot(page, name: str) -> Path | None:
    try:
        SHOT_DIR.mkdir(exist_ok=True)
        p = SHOT_DIR / f"{dt.datetime.now():%Y%m%d-%H%M%S}-{name}.png"
        page.screenshot(path=str(p), full_page=True)
        return p
    except Exception:
        return None


# ------------------------------------------------------------ page actions
def select_service(page, group: str, service: str) -> None:
    """Open the service group (e.g. 'Aufenthaltserlaubnis') and press + once."""
    page.get_by_role("heading", name=group, exact=True).first.click()
    row = page.get_by_role("checkbox", name=re.compile("^" + re.escape(service) + r"\b"))
    plus = row.get_by_role("button", name="Plus Zeichen").first
    try:
        plus.click(timeout=8000)
    except PWTimeout:
        # Fallback: find the service text and the nearest '+' button after it.
        label = page.get_by_text(service, exact=True).first
        label.locator("xpath=ancestor::*[.//button][1]").get_by_role(
            "button", name="Plus Zeichen"
        ).first.click(timeout=8000)


def open_calendar_and_get_first_day(page) -> dt.date | None:
    """Click 'Weiter zur Terminauswahl'. The page jumps to the earliest free day
    and loads its times; we read that day from the timeslots request."""

    def is_day_request(r):
        return "/api/timeslots" in r.url and "rangesearch=1" not in r.url

    try:
        with page.expect_response(is_day_request, timeout=25000) as resp_info:
            page.get_by_role("button", name="Weiter zur Terminauswahl").click()
        resp = resp_info.value
    except PWTimeout:
        return None  # no free day at all in the booking window

    q = urllib.parse.parse_qs(urllib.parse.urlparse(resp.url).query)
    day = dt.date.fromisoformat(q["date"][0])
    try:
        slots = resp.json()
        log(f"Earliest free day: {day} ({len(slots)} time slots listed)")
    except Exception:
        log(f"Earliest free day: {day}")
    return day


def pick_first_time(page) -> str:
    slot = page.get_by_text(TIME_SLOT_RE).first
    slot.wait_for(state="visible", timeout=15000)
    text = slot.inner_text().strip()
    slot.click()
    return text


def fill_form(page, me: dict) -> None:
    page.get_by_role("heading", name="Ihre Daten").wait_for(state="visible", timeout=15000)
    page.select_option("#Salutation", label=me["salutation"])
    page.fill("#FirstName", me["first_name"])
    page.fill("#LastName", me["last_name"])
    page.fill("#Birthday", me["birthday"])          # <input type=date> takes YYYY-MM-DD
    page.fill("#Street", me.get("street", ""))
    page.fill("#ZIP", me.get("zip", ""))
    page.select_option("#City", label=me["city"])
    page.fill("#Phone", me["phone"])
    page.fill("#Email", me["email"])
    if me.get("booking_for"):
        page.fill("#Additional2", me["booking_for"])
    if me.get("remarks"):
        page.fill("#Additional3", me["remarks"])
    # Privacy-notice acknowledgement (required). Never touch the hidden
    # 'website_url_hp' field – it is a bot trap.
    page.locator("#chkdp").check(force=True)


def summary_date(page) -> dt.date | None:
    m = SUMMARY_DATE_RE.search(page.inner_text("body"))
    return dt.datetime.strptime(m.group(1), "%d.%m.%Y").date() if m else None


def submit_and_confirm(page) -> bool:
    page.locator("#cmdBookAppointment").click()
    deadline = time.time() + 40
    while time.time() < deadline:
        if page.get_by_text("Der Termin wurde nicht gebucht").first.is_visible():
            return False
        if page.get_by_role("button", name="Weiteren Termin buchen").first.is_visible():
            return True
        if page.get_by_text(re.compile(r"Vielen Dank|erfolgreich gebucht|Buchungsbestätigung")).first.is_visible():
            return True
        # A validation message (e.g. bad phone format) keeps us on the form.
        page.wait_for_timeout(1000)
    return False


# ------------------------------------------------------------- one check
def check_once(cfg: dict, headless: bool) -> int:
    if LOCK_FILE.exists():
        log(f"Already booked earlier ({LOCK_FILE.name}). Not booking again.")
        return 10

    search = cfg["search"]
    cutoff = dt.date.fromisoformat(search["book_before"])
    dry_run = bool(cfg.get("dry_run", True))

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page(locale="de-DE", timezone_id="Europe/Berlin")
        try:
            page.goto(URL, wait_until="networkidle", timeout=60000)
            select_service(page, search["service_group"], search["service"])
            day = open_calendar_and_get_first_day(page)

            if day is None:
                log("No free appointments in the booking window.")
                return 0
            if day >= cutoff:
                log(f"Earliest is {day} – not before {cutoff}. Waiting.")
                return 0

            # ---- an earlier slot exists -------------------------------------------
            time_text = pick_first_time(page)
            log(f"Earlier slot found: {day} {time_text}")
            page.get_by_role("button", name="Weiter zur Dateneingabe").click()
            fill_form(page, cfg["person"])

            chosen = summary_date(page)
            if chosen is None or chosen >= cutoff:
                notify(cfg, "Termin-Bot: check needed",
                       f"Saw {day} {time_text} but the summary showed {chosen}. Did not book.")
                shot(page, "summary-mismatch")
                return 2

            if dry_run:
                path = shot(page, "dry-run-form")
                notify(cfg, "Termin-Bot DRY RUN: slot found",
                       f"{chosen:%d.%m.%Y} {time_text} is free. Dry run, so NOT booked. "
                       f"Screenshot: {path}. Book it yourself now, or set dry_run = false.",
                       priority="high")
                return 0

            ok = submit_and_confirm(page)
            path = shot(page, "booked" if ok else "booking-failed")
            if ok:
                LOCK_FILE.write_text(json.dumps(
                    {"date": chosen.isoformat(), "time": time_text,
                     "booked_at": dt.datetime.now().isoformat(timespec="seconds")}, indent=2))
                notify(cfg, "Termin BOOKED ✔",
                       f"Booked {chosen:%d.%m.%Y} {time_text} at Ausländerbehörde Heilbronn. "
                       "Check your e-mail for the confirmation, then CANCEL your May appointment "
                       "(link in its confirmation e-mail) – the office cancels duplicates.",
                       priority="urgent")
                return 10
            notify(cfg, "Termin-Bot: booking failed",
                   f"Tried {chosen:%d.%m.%Y} {time_text} but no confirmation appeared "
                   f"(slot may have been taken). Screenshot: {path}. Will keep trying.",
                   priority="high")
            return 0
        except Exception:
            log("Error during check:\n" + traceback.format_exc())
            shot(page, "error")
            return 2
        finally:
            browser.close()


# ------------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(HERE / "config.toml"))
    ap.add_argument("--once", action="store_true", help="run a single check and exit")
    ap.add_argument("--show", action="store_true", help="show the browser window")
    args = ap.parse_args()

    cfg = load_config(Path(args.config))
    headless = not args.show and cfg.get("headless", True)
    log(f"Mode: {'DRY RUN (will not book)' if cfg.get('dry_run', True) else 'LIVE (will book)'}; "
        f"looking for {cfg['search']['service']} before {cfg['search']['book_before']}")

    if args.once:
        sys.exit(check_once(cfg, headless))

    every = int(cfg["search"].get("check_every_minutes", 5))
    night = int(cfg["search"].get("night_check_every_minutes", 15))
    errors = 0
    notify(cfg, "Termin-Bot started", "Watching for an earlier appointment.", priority="low")
    while True:
        code = check_once(cfg, headless)
        if code == 10:
            log("Done – appointment booked. Exiting.")
            return
        errors = errors + 1 if code == 2 else 0
        if errors == 3:
            notify(cfg, "Termin-Bot: repeated errors",
                   "3 checks in a row failed – the site may have changed or you are offline. "
                   "See the terminal / screenshots folder.", priority="high")
        hour = dt.datetime.now().hour
        minutes = night if hour < 6 else every
        wait = minutes * 60 + random.randint(-30, 30)
        time.sleep(max(60, wait))


if __name__ == "__main__":
    main()
