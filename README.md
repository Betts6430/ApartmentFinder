# ApartmentFinder

Edmonton-focused rental aggregator. Scrapes popular rental sites on demand, applies filters (including transit-time to a chosen landmark), and ranks results by Best Value, Best Location, and Nicest Places.

## Sources
- RentFaster
- Rentals.ca
- Zumper
- (planned) Apartments.com, Zillow

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

## Architecture
- FastAPI single-process app, Jinja2 templates + HTMX + Tailwind via CDN
- SQLite cache for scraped listings (~3h TTL) and Google Maps transit times (indefinite)
- Scrapers run on demand when a search is submitted
