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
  models.py          PropertyType/SortBy enums; SearchFilters + Listing models; Listing.matches();
                     normalize_address() + address field validator (one house style)
  cache.py           SQLite cache: listings, search_cache, transit_cache, geocode_cache,
                     favorites, listing_seen, meta, contact_cache, price_history
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
  templates/         base.html (header + "Saved" nav + global delegated JS for hearts/contact/copy),
                     index.html (search form + autocomplete), results.html (Grid/List/Map views,
                     pagination), favorites.html (saved listings), _macros.html (shared card/row +
                     fav_button / new_badge / contact_block / contact_panel / specs / amenities_str),
                     _contact_panel.html (fragment returned by the lazy phone endpoint)
  static/            (empty; mounted at /static)
data/cache.db        SQLite cache (gitignored)
run.sh, requirements.txt
.env                 secrets (gitignored) — see below
```

### Routes (`app/main.py`)
- `GET /` — search form
- `POST /search` — runs a search, returns results HTML
- `GET /favorites` — saved-listings page
- `POST /api/favorites/toggle` (form `id`) — toggle a listing's saved state; returns
  `{"favorited": bool}`. Snapshots the listing on save (see below).
- `GET /api/listings/{id}/phone` — lazily resolve a listing's contact number (Zumper:
  fetch its detail page) and return the rendered contact-panel HTML (or a link-out
  fallback) to swap in. Result cached forever in `contact_cache` (misses too).
- `GET /api/places/autocomplete?q=` — **backend proxy** to Places API (New) for the
  location autocomplete; returns `{"suggestions": [str, ...]}`. Key stays server-side.

## Key architecture decisions

- **Single pool cache key.** The whole scraped Edmonton pool is cached under ONE
  constant key (`edmonton:pool:v6`, see `SearchFilters.scrape_cache_key()`), NOT
  per filter combo. Filters run in-memory against the cached pool. So only the
  first scrape is slow (~3–10s); later searches with different filters are <200ms.
  TTL = `SEARCH_CACHE_TTL_HOURS` (default 3h). **Bump the `vN` suffix whenever the
  shape/processing of the cached pool changes** (e.g. dedupe added at v2; proximity
  dedupe + sqft sanitizing at v3; `phone` field at v4; phone for Rentals.ca/Zumper
  at v5; `phone_ext` field at v6) so stale pools get re-scraped instead of waiting
  out the TTL.
- **Cross-source dedupe.** The same posting often appears on multiple sites. After
  the per-`id` dedupe, `_scrape_all` collapses these via `_dedupe_cross_source`
  (`services/search.py`) **before caching**, so the cached pool is already clean.
  Listings are bucketed by `(round(price), bedrooms, bathrooms)`, then within a
  bucket greedily clustered by location: same coords within **`_DUP_RADIUS_KM`
  (~200m, haversine)** when both have coords, else a matching normalized street
  address. (Proximity, not a rounded grid — different sources geocode the same
  building ~100m apart, and grid rounding split those across cell boundaries,
  leaving dupes. v2 used a 3-decimal grid; v3 uses true distance.) Listings with
  no usable location signal are kept as-is. The **richest** copy wins
  (`_completeness`: has-phone → photos → has-coords → has-address → description
  length → amenity count — has-phone is first so dedupe keeps the contactable
  copy). Tradeoff: two distinct units in one building sharing identical
  price/beds/baths within ~200m collapse to one.
- **Square footage is sanitized** at scrape time via `scrapers/base.sane_sqft`:
  values outside 50–50000 sqft (e.g. Zumper's int64 `9223372036854775807`
  "unknown" sentinel, or a stray `1`) become `None` rather than displaying as junk.
- **Contact button.** `scrapers/base.normalize_phone` parses a raw number into
  `(Listing.phone, Listing.phone_ext)` — a 10/11-digit NANP base plus any leftover
  digits as an extension, so call-center / property-manager numbers like
  "(844) 332-5934 ext. 4525" are kept (not dropped). **RentFaster** (~95%) and
  **Rentals.ca** (~30%, `node.contact.phoneNumber`) expose the number in their
  search feed, so it's captured at scrape time. **Zumper** almost never includes it
  in the feed — the number lives on the per-listing **detail page**
  (`detail.entity.data.agents[].phone`, often the agent's direct line), so it's
  resolved **lazily on demand**: the contact button for a Zumper listing reads
  "Show phone number" and, on click, hits `GET /api/listings/{id}/phone`, which
  fetches+parses the detail page, caches the result forever in `contact_cache`
  (misses included, as an empty `phone`, so we never re-fetch), and returns the
  rendered panel (or link-out) HTML to swap in. This keeps the bulk scrape fast and
  spends a request only on listings the user actually pursues (the *lazy on-click*
  choice over eager per-page / bulk-on-scrape).
  The panel markup lives in **one place** — the `contact_panel` Jinja macro in
  `_macros.html`, rendered both server-side (behind a "Contact" toggle for
  known-phone listings) and by the endpoint via the `_contact_panel.html` partial.
  It's a **floating popover** (absolute, so opening it never grows the card/row;
  the macro's `align` arg anchors it left for grid, right for list — and the
  card/list containers drop `overflow-hidden` so it isn't clipped). It shows the
  formatted number + extension (`tel:` link; ext becomes a `tel:...,EXT` pause) and
  a pre-written availability + viewing message with a single **send-icon** button
  in the textarea corner that opens the `sms:` draft. Listings with no usable number
  get a secondary "Contact on {source} →" link-out. No messages are ever sent
  automatically — it only drafts/opens; the user sends. The click handlers (panel
  toggle and the lazy phone-fetch) are **global delegated listeners in `base.html`**,
  shared by results + favorites.
- **Saved listings (favorites).** A ♡/♥ icon button on every card/row toggles
  `POST /api/favorites/toggle` (delegated JS in `base.html`). On save the **full
  listing is snapshotted** into the `favorites` table (`Listing.model_dump_json`),
  so a favorite survives the pool cache expiring or the listing being delisted at
  the source. `GET /favorites` renders the snapshots, newest-first. Toggling off on
  the favorites page removes the card from the DOM client-side.
- **"New" listing badges.** `cache.record_scrape()` (called by `run_search` after a
  fresh scrape) stamps first-seen times in `listing_seen` and advances a scrape
  boundary in the `meta` k/v table; `get_new_ids()` flags listings first seen since
  the **previous** scrape. Nothing is flagged on the very first scrape (avoids a
  cold-start where everything looks new), and badges only appear on a fresh scrape,
  not a cached-pool hit.
- **Price-drop tracking.** `cache.record_prices()` (also called after a fresh scrape)
  appends to `price_history` **only when a listing's price changed** since its last
  recorded point (or it's new) — one row per change, not per scrape, so the table
  stays compact. On every search `run_search` enriches survivors with
  `cache.get_price_drops()`, which compares each listing's two most recent points and
  returns the prior price when the **latest change was a drop within 30 days**; that
  populates the enriched `Listing.prev_price`. A rose **"↓ $X"** badge
  (`price_drop_badge` macro) then shows on cards/rows, and the **"Price drops"** sort
  (`SortBy.PRICE_DROP`, ranks by `prev_price - price`) surfaces the biggest reductions.
  Drops only materialize once a price actually changes between two scrapes; the first
  scrape just seeds the baseline.
- **Address normalization.** Sources spell addresses inconsistently
  (`St`/`Street`/`ST`, `NW`/`Northwest`, ALL CAPS, ordinal `37th`). `normalize_address()`
  + a `Listing` **field validator** (`models.py`) coerce one house style: street
  types spelled out, uppercase quadrant abbreviations, ordinals stripped from
  numbered streets, everything else Title Cased. Because it's a model validator it
  runs on **every** `Listing` — fresh scrapes *and* ones re-loaded from cache /
  favorites — so existing cached data displays consistently with **no re-scrape**.
  Per-listing titles/blurbs are no longer shown (mostly repetitive boilerplate).
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

## Status (as of 2026-06-25)

Working: all three scrapers, pool caching, ranking, blank-field-tolerant search,
commute filter (geocode + Distance Matrix), location autocomplete, cross-source
dedupe, paginated results with Grid/List/Map views, the contact button, saved-listing
favorites, "New" listing badges, address normalization, and price-drop tracking.

Recent fixes: blank optional numeric fields no longer 422; Zumper unknown-pets bug;
batched transit cache lookups; dead-code cleanup; location autocomplete;
cross-source dedupe of duplicate postings; search-form UI refresh (sectioned
card, logo header + footer in `base.html`); reject out-of-Edmonton geocodes (with
a results-page warning banner); **server-side pagination (60/page)** with
filter-preserving sort/page nav; **List view** (`results.html`) alongside Grid/Map;
**sqft sanitizing** + **proximity-based dedupe** (v4 pool); **contact button**
(RentFaster phone → message draft; others link out); **saved-listing favorites**
(snapshotted) + **"New" badges**; **address normalization** (one house style via a
model validator); listing UI cleanup (dropped repetitive titles, buttonified
save/contact actions, compacted List view; shared rendering moved to `_macros.html`);
**phone capture for Rentals.ca** via shared
`normalize_phone`, so the contact panel now appears for many more listings;
**lazy on-demand Zumper phone lookup** (detail-page fetch, cached in `contact_cache`)
behind a "Show phone number" button; `normalize_phone` now keeps extensions
(`phone_ext`), so call-center/PM numbers aren't dropped (v6 pool); contact panel,
list-row, and view-toggle UI polish (send-icon button, image-height rows, icon
toggles); **price-drop tracking** ("↓ $X" badge + "Price drops" sort via
`price_history`).

### Possible next steps (not started)
- Cache `/api/places/autocomplete` responses (currently every keystroke-after-debounce
  hits Google; cheap but not free).
- Apartments.com / Zillow scrapers (deferred — both Cloudflare-hard). Facebook
  Marketplace explicitly skipped per user.
- Persist/share searches.
- Consider Routes API migration only if legacy Distance Matrix gets sunset (mind the
  transit caveat above).
```
