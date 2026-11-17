# Heilbronn Ausländerbehörde: earlier-appointment bot

This bot watches the city's eTermin booking page
(https://www.etermin.net/qtermin-stadtheilbronn-abh). When a slot **before
1 May 2027** frees up, it books the slot with your details and sends a
notification to your phone.

The earliest free date on 17 Nov 2026 was 2 June 2027, and the booking window
reaches about 270 days ahead. Earlier slots will therefore almost only come from
other people's cancellations. These can appear at any time and are usually gone
within minutes, so the bot checks every 5 minutes.

## ⚠️ Read this first
- **The office cancels duplicate bookings.** The booking page says they cancel
  appointments when someone books several for the same service. When the bot
  books an earlier slot, **cancel your May appointment right away** using the link
  in its confirmation e-mail.
- Set `service` in the config to **the same service you booked in May**
  (Erteilung vs. Verlängerung). The office also cancels wrongly chosen services.
- The bot books only once. After a booking it writes `booked.json` and stops.
  Delete that file only if you want it to search again.

## Setup (Mac or Windows, about 10 minutes)
1. Install Python 3.11 or newer.
2. In this folder, run:
   ```bash
   python -m venv .venv
   source .venv/bin/activate        # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   python -m playwright install chromium
   ```
3. Copy `config.example.toml` to `config.toml` and fill in your details.
4. **Phone alerts (recommended):** install the free **ntfy** app and subscribe to a
   topic with a long random name. Put that name in `ntfy_topic`.

## Run it
1. **Test run, watching the browser** (`dry_run = true`, which is the default):
   ```bash
   python appointment_bot.py --once --show
   ```
   You'll see it pick the service and open the calendar. Today it will only say
   "Earliest is 2027-06-… – not before 2027-05-01". That is expected.
   To test the form-filling step as well, set `book_before = "2027-12-31"`
   **while `dry_run` is still true**, run it again, and check the screenshot in
   `screenshots/`. Then set `book_before` back.
2. **Go live:** set `dry_run = false` and start the loop:
   ```bash
   python appointment_bot.py
   ```
   Leave the terminal open. The computer must stay awake and online. On a Mac,
   `caffeinate -i python appointment_bot.py` keeps it awake.

## Optional: run in the cloud with GitHub Actions
If you don't want to keep your laptop on, use GitHub Actions.
1. Push this folder to a **public** GitHub repo. `config.toml` is git-ignored,
   so it won't be uploaded.
2. Go to Settings → Secrets → Actions → New secret. Name it `CONFIG_TOML` and
   paste your whole `config.toml` (with `dry_run = false`).
3. The workflow checks about every 10 minutes. GitHub often starts these runs
   late, so it reacts more slowly than the laptop version. After a booking, the
   workflow switches itself off.

## If something breaks
- If 3 checks in a row fail, you get a "repeated errors" alert, and screenshots
  are saved in `screenshots/`. Usually this means eTermin changed its page.
- Run with `--show` to watch where it gets stuck.
