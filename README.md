# ApartmentFinder

Edmonton-focused rental aggregator. Scrapes popular rental sites on demand, applies filters (including transit-time to a chosen landmark), and ranks results by Best Value, Best Location, and Nicest Places.

## Sources
- RentFaster
- Rentals.ca
- Zumper
- Kijiji
- (planned) Apartments.com, Zillow

## Features
- Three rankings (Best Value / Best Location / Nicest Places) over a deduped, cross-source pool
- Optional commute-time filter + ranking to any location (e.g. University of Alberta)
- Grid / List / Map result views with pagination
- Contact button (phone + pre-drafted SMS/`tel:` where a number is available)
- Saved listings (favorites) and saved searches with new-match counts
- "New" listing badges and price-drop tracking
- **Saved-search email alerts** — get emailed when a saved search gets new matches ([setup](#email-alerts-optional))

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env and set GOOGLE_MAPS_API_KEY (see below)
./run.sh
```

Then open http://localhost:8000.

### Google Maps API key

In Google Cloud Console:
1. Create a project, then under "APIs & Services" enable:
   - **Maps JavaScript API** (for the in-app map view)
   - **Geocoding API** (for resolving "University of Alberta" to coordinates)
   - **Distance Matrix API** (for transit-time filter)
2. Create an API key, restrict it to those three APIs.
3. Set HTTP referrer restriction to `http://localhost:8000/*` for personal use.
4. Paste the key into `.env` as `GOOGLE_MAPS_API_KEY=...`.

The app works without a key — the commute filter and map view are simply disabled.

(The location autocomplete additionally uses the **Places API (New)**; enable it on the
same project if you want address suggestions.)

### Email alerts (optional)

Get emailed when a saved search picks up new matches. Configure the **sending mailbox**
in `.env`, then set the **recipient** in-app on the Settings page (`/settings`).

```env
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=your-sender@gmail.com
SMTP_PASSWORD=your-app-password      # Gmail: an App Password, not your login password
SMTP_FROM=your-sender@gmail.com      # optional; defaults to SMTP_USER
ALERT_POLL_MINUTES=180               # background re-scrape cadence; 0 disables auto-checks
# ALERT_EMAIL_TO=...                  # optional recipient fallback; prefer the Settings page
```

For Gmail you must enable 2-Step Verification and create an [App Password](https://myaccount.google.com/apppasswords).
After editing `.env`, **restart the app** (the `--reload` watcher doesn't pick up `.env` changes).
On the Settings page, enter your recipient email, Save, then use **Send test email** to verify.
Alerts no-op until SMTP is configured; the whole feature is opt-in.

## Architecture
- FastAPI single-process app, Jinja2 templates + Tailwind (CDN) + vanilla JS
- SQLite cache for scraped listings (~3h TTL) and Google Maps transit times (indefinite)
- Scrapers run on demand when a search is submitted
- Saved-search alerts dispatch after a fresh scrape; a background poller forces periodic re-scrapes so alerts arrive while the app is idle
