# CLAUDE.md — ApartmentFinder

Context for Claude Code sessions on this project. Read this first.

## What this is

A **personal-use, Edmonton-focused rental aggregator**. It scrapes RentFaster,
Rentals.ca, and Zumper on demand, normalizes the listings, applies in-memory
filters, ranks them three ways (Best Value / Best Location / Nicest Places), and
optionally filters/ranks by **commute time** to a location (e.g. University of
Alberta) via Google Maps.

Single-user, single-process. Cost target is **$0**: scrapers run on-demand from
the user's own machine (no VPS), and Google Maps usage stays within the free
tier thanks to aggressive caching.

## Tech stack

- Python 3.12 + **FastAPI** + **Jinja2** templates + **Tailwind (CDN)** + vanilla JS
- **SQLite** (via `aiosqlite`) for all caching
- **curl_cffi** for TLS-fingerprint scraping past Cloudflare (RentFaster / Rentals.ca)
- **httpx** for Google Maps API calls (Geocoding, Distance Matrix, Places New)
- pydantic v2 + pydantic-settings

> Note: `requirements.txt` lists `htmx` historically but it is **not used** — the
> UI is plain server-rendered forms + vanilla JS. (The dead htmx/Alpine CDN
> includes were removed from `base.html`.)

## How to run

```bash
./run.sh          # activates .venv, runs uvicorn app.main:app on 0.0.0.0:8000 --reload
```

Then open **http://localhost:8000**. Stop with `pkill -f "uvicorn app.main:app"`.

`--reload` is on, so Python/template edits apply live.

### Environment gotchas (important)
- **No sudo, no Node, `python3-venv` not installed.** The `.venv` was bootstrapped
  via `get-pip.py` into a `--without-pip` venv. Don't suggest installing system
  packages — work within Python user-space.
- **`python` is NOT on PATH** unless the venv is active. For one-off scripts:
  `source .venv/bin/activate` first, or call `.venv/bin/python` directly.

## Layout

```
app/
  main.py            FastAPI app + routes (see below)
  config.py          pydantic-settings; reads .env (GOOGLE_MAPS_API_KEY, CACHE_DIR, SEARCH_CACHE_TTL_HOURS)
  models.py          PropertyType/SortBy enums; SearchFilters + Listing models; Listing.matches()
  cache.py           SQLite cache: listings, search_cache, transit_cache, geocode_cache
  scrapers/
    base.py          Scraper ABC
    __init__.py      SCRAPERS registry (RentFaster, RentalsCa, Zumper)
    rentfaster.py    public map.json endpoint (structured JSON)
    rentals_ca.py    scrapes inline `App.store.search = {response: {...}}` JSON, paginates
    zumper.py        scrapes inline `window.__PRELOADED_STATE__` JSON, paginates
  services/
    search.py        run_search(): cache -> scrape -> filter -> transit enrich -> filter -> rank
    ranking.py       value/location/niceness scores + sort_listings()
    transit.py       geocode() + compute_transit() (Google Maps)
  templates/         base.html, index.html (search form + autocomplete), results.html (cards + map)
  static/            (empty; mounted at /static)
data/cache.db        SQLite cache (gitignored)
run.sh, requirements.txt
.env                 secrets (gitignored) — see below
```

### Routes (`app/main.py`)
- `GET /` — search form
- `POST /search` — runs a search, returns results HTML
- `GET /api/places/autocomplete?q=` — **backend proxy** to Places API (New) for the
  location autocomplete; returns `{"suggestions": [str, ...]}`. Key stays server-side.

## Key architecture decisions

- **Single pool cache key.** The whole scraped Edmonton pool is cached under ONE
  constant key (`edmonton:pool:v2`, see `SearchFilters.scrape_cache_key()`), NOT
  per filter combo. Filters run in-memory against the cached pool. So only the
  first scrape is slow (~3–10s); later searches with different filters are <200ms.
  TTL = `SEARCH_CACHE_TTL_HOURS` (default 3h). **Bump the `vN` suffix whenever the
  shape/processing of the cached pool changes** (e.g. dedupe was added at v2) so
  stale pools get re-scraped instead of waiting out the TTL.
- **Cross-source dedupe.** The same posting often appears on multiple sites. After
  the per-`id` dedupe, `_scrape_all` collapses these via `_dedupe_cross_source`
  (`services/search.py`) **before caching**, so the cached pool is already clean.
  Merge key = `(round(price), bedrooms, bathrooms, location)` where location is
  GPS coords rounded to ~100m (3 decimals) when present, else a normalized street
  address; listings with neither are never merged (kept as-is). When duplicates
  collide, the **richest** copy wins (`_completeness`: photos → has-coords →
  has-address → description length → amenity count). Tradeoff: two distinct units
  in one building sharing identical price/beds/baths within ~100m collapse to one.
- **Filters are optional.** `SearchFilters` fields are all `Optional`; `matches()`
  treats `None` as "don't filter on this." Empty form fields must map to `None`
  (see "blank field" note below).
- **Transit is a two-phase filter.** `run_search` applies all non-transit filters
  first (cheap), then only geocodes + computes commute times for survivors, then
  applies `transit_minutes_max`. Listings without lat/lng are dropped when a
  commute filter is active (can't be scored).
- **Transit cache never expires**, keyed by a hash of (origin, dest, mode) rounded
  to 5 decimals. Lookups are **batched** in one SQL query
  (`cache.get_cached_transit_many`).

## Google Maps API — READ THIS (it caused a lot of pain)

The app touches **three** Google APIs. They must all be enabled **on the same
Cloud project the API key belongs to**, and the key must not be API-restricted
away from them.

| Feature | Code | Endpoint | API to enable |
|---|---|---|---|
| Geocode location | `transit.py geocode()` | `maps/api/geocode/json` (legacy) | **Geocoding API** |
| Commute times | `transit.py compute_transit()` | `maps/api/distancematrix/json` (legacy) | **Distance Matrix API** |
| Address autocomplete | `main.py places_autocomplete()` | `places.googleapis.com/v1/places:autocomplete` (NEW) | **Places API (New)** |

### Hard-won lessons
- **Legacy APIs are gated on newer Cloud projects.** Symptom: `REQUEST_DENIED` +
  "You're calling a legacy API, which is not enabled... switch to Places API (New)
  or Routes API" (`LegacyApiNotActivatedMapError`). Fix = enable that specific API
  **on the key's project** (and wait a few min to propagate). If the project can't
  enable it at all, you must migrate to the new API.
- **Distance Matrix (legacy) currently WORKS** for the active key. If Google ever
  fully sunsets it, migration target is the **Routes API** — but note its
  `computeRouteMatrix` does **NOT support transit mode** (only drive/walk/bike).
  Transit would need per-listing `computeRoutes` calls or be dropped. This is why
  we kept the legacy Distance Matrix endpoint.
- **Autocomplete uses Places API (New), not the legacy widget.** An earlier attempt
  with `google.maps.places.Autocomplete` (legacy JS widget) both (a) required the
  gated legacy Places API and (b) on auth failure injected an overlay that LOCKED
  the input so you couldn't type. We replaced it with a **backend proxy + custom
  vanilla-JS dropdown** (debounced, 3-char min, Edmonton-biased, arrow/enter/esc
  nav). The plain `<input>` is never touched by third-party code, so it always
  stays usable as a fallback.
- `API_KEY_SERVICE_BLOCKED` / `PERMISSION_DENIED` on the new Places API = the
  **key's API restrictions** block it. Fix in Credentials → key → API restrictions
  (add the API, or "Don't restrict key").
- **Graceful degradation:** if `GOOGLE_MAPS_API_KEY` is empty or any Maps call
  fails, geocode/transit return `None` and autocomplete returns `[]` — the app
  still works, just without commute features.

## Secrets / .env

`.env` (gitignored) holds:
```
GOOGLE_MAPS_API_KEY=...
CACHE_DIR=./data
SEARCH_CACHE_TTL_HOURS=3
```
Update the key by editing `.env` directly (don't paste secrets into chat). The
running app must be restarted (or it auto-reloads) to pick up a new key.

## Git / pushing

- Remote: `origin` = `git@github-personal:Betts6430/ApartmentFinder.git`
  (personal GitHub account **Betts6430**).
- Pushes use a **dedicated SSH key via host alias** so they don't collide with the
  user's org GitHub identity (`ualberta-baymax`):
  - `~/.ssh/config` has `Host github-personal` → `IdentityFile ~/.ssh/id_ed25519_personal`
  - `ssh -T git@github-personal` should greet **Betts6430**.
- `git push origin main` just works from this repo. Default branch is `main`.
- `.gitignore` excludes `.env`, `.venv/`, `data/`, `.claude/settings.local.json`.

## Status (as of 2026-06-24)

Working: all three scrapers, pool caching, ranking, blank-field-tolerant search,
commute filter (geocode + Distance Matrix), location autocomplete, and
cross-source dedupe.

Recent fixes: blank optional numeric fields no longer 422; Zumper unknown-pets bug;
batched transit cache lookups; dead-code cleanup; location autocomplete;
cross-source dedupe of duplicate postings; search-form UI refresh (sectioned
card, logo header + footer in `base.html`).

### Possible next steps (not started)
- Cache `/api/places/autocomplete` responses (currently every keystroke-after-debounce
  hits Google; cheap but not free).
- Apartments.com / Zillow scrapers (deferred — both Cloudflare-hard). Facebook
  Marketplace explicitly skipped per user.
- Persist/share searches; saved-listing favorites.
- Consider Routes API migration only if legacy Distance Matrix gets sunset (mind the
  transit caveat above).
```
