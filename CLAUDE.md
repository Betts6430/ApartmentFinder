# CLAUDE.md — ApartmentFinder

Context for Claude Code sessions on this project. Read this first.

## What this is

A **personal-use, Edmonton-focused rental aggregator**. It scrapes RentFaster,
Rentals.ca, Zumper, and Kijiji on demand, normalizes the listings, applies in-memory
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

> Note: the UI is plain server-rendered forms + vanilla JS — **no htmx/Alpine**
> (the dead htmx/Alpine CDN includes were removed from `base.html`, and `htmx` is
> no longer in `requirements.txt`).

## How to run

```bash
./run.sh          # activates .venv, runs uvicorn app.main:app on 0.0.0.0:8000 --reload
```

Then open **http://localhost:8000**. Stop with `pkill -f "uvicorn app.main:app"`.

`--reload` is on, so Python/template edits apply live. **`.env` changes are NOT
watched** — restart the app to pick those up.

## Tests

```bash
./run_tests.sh            # pytest over tests/ (pass-through args ok: ./run_tests.sh -k phone)
```

Unit tests cover the pure logic (cross-source dedupe, address/phone normalization,
sqft sanitizing, ranking/sorting incl. the first_seen-based "Newest", `matches()`,
transit departure-time helper). `run_tests.sh` sets `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`
to isolate from unrelated **system pytest plugins** (ROS's `launch_testing` leaks in via
global site-packages and fails to import `lark`) — run pytest that way, not bare.

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
  config.py          pydantic-settings; reads .env (GOOGLE_MAPS_API_KEY, CACHE_DIR, SEARCH_CACHE_TTL_HOURS,
                     SMTP_* + ALERT_EMAIL_TO + ALERT_POLL_MINUTES for saved-search alerts)
  models.py          PropertyType/SortBy enums; SearchFilters + Listing models; Listing.matches();
                     normalize_address() + address field validator (one house style)
  cache.py           SQLite cache: listings, search_cache, transit_cache, geocode_cache,
                     favorites, listing_seen, meta (get_meta/set_meta k/v; holds the alert
                     recipient), contact_cache, price_history, saved_searches (+ last_alerted_at)
  scrapers/
    base.py          Scraper ABC
    __init__.py      SCRAPERS registry (RentFaster, RentalsCa, Zumper)
    rentfaster.py    public map.json endpoint (structured JSON)
    rentals_ca.py    scrapes inline `App.store.search = {response: {...}}` JSON, paginates
    zumper.py        scrapes inline `window.__PRELOADED_STATE__` JSON, paginates
    kijiji.py        scrapes inline `__NEXT_DATA__` Apollo cache (RealEstateListing), paginates
  services/
    search.py        run_search(): cache -> scrape -> filter -> transit enrich -> filter -> rank
    ranking.py       value/location/niceness scores + sort_listings()
    transit.py       geocode() + compute_transit() (Google Maps)
    alerts.py        dispatch_alerts() emails new saved-search matches; alert_poller() background re-scrape
  templates/         base.html (header + "Saved" nav + global delegated JS for hearts/contact/copy),
                     index.html (search form + autocomplete), results.html (Grid/List/Map views,
                     pagination + Save-search), favorites.html (saved listings),
                     searches.html (saved searches + new-match counts),
                     settings.html (alert-recipient email + SMTP status), _macros.html (shared card/row +
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
- `GET /searches` — saved-searches page (each with a new-match count)
- `POST /api/searches` (form `name`, `filters` = SearchFilters JSON) — save a search
- `GET /searches/{id}/open` — run a saved search (renders results.html) + mark viewed
- `POST /searches/{id}/delete` — delete a saved search (303 redirect)
- `GET /settings` — settings page (alert-recipient email + SMTP/poller status)
- `POST /settings` (form `alert_email`) — save the alert recipient to the DB (blank
  clears it; invalid email re-renders 400); 303 redirect to `/settings?saved=1`
- `POST /api/settings/test-email` — send a test email to the saved recipient
  (`alerts.send_test_email`); returns `{"ok": true, "to": ...}` or `{"ok": false, "error": ...}`
- `GET /api/listings/{id}/phone` — lazily resolve a listing's contact number (Zumper:
  fetch its detail page) and return the rendered contact-panel HTML (or a link-out
  fallback) to swap in. Result cached forever in `contact_cache` (misses too).
- `GET /api/places/autocomplete?q=` — **backend proxy** to Places API (New) for the
  location autocomplete; returns `{"suggestions": [str, ...]}`. Key stays server-side.

## Key architecture decisions

- **Single pool cache key.** The whole scraped Edmonton pool is cached under ONE
  constant key (`edmonton:pool:v7`, see `SearchFilters.scrape_cache_key()`), NOT
  per filter combo. Filters run in-memory against the cached pool. So only the
  first scrape is slow (~3–10s); later searches with different filters are <200ms.
  TTL = `SEARCH_CACHE_TTL_HOURS` (default 3h). **Bump the `vN` suffix whenever the
  shape/processing of the cached pool changes** (e.g. dedupe added at v2; proximity
  dedupe + sqft sanitizing at v3; `phone` field at v4; phone for Rentals.ca/Zumper
  at v5; `phone_ext` field at v6; Kijiji source added at v7) so stale pools get
  re-scraped instead of waiting out the TTL.
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
- **Kijiji scraper quirks.** `kijiji.py` reads the `__NEXT_DATA__` Apollo cache
  (`RealEstateListing` entries) from the Edmonton apartments-condos category
  (`c37l1700203` — its feed mixes in house/townhouse/duplex unit types too, ~1700
  listings). Decode gotchas, all handled in `_parse_listing`: `price.amount` is in
  **cents** (÷100), `numberbathrooms` is encoded **×10** ("15"→1.5 baths), structured
  fields live in `attributes.all` as canonicalName/canonicalValues, and "TOP_AD"
  promoted listings repeat across pages (deduped by id). Page 1 reports
  `pagination.totalCount`, so we fetch exactly ⌈total/40⌉ pages (capped at 45)
  concurrently. `_split_address` strips province/postal and keeps a street only when
  it contains a digit (private posters hide the street, leaving just "Edmonton, AB").
- **Contact button.** `scrapers/base.normalize_phone` parses a raw number into
  `(Listing.phone, Listing.phone_ext)` — a 10/11-digit NANP base plus any leftover
  digits as an extension, so call-center / property-manager numbers like
  "(844) 332-5934 ext. 4525" are kept (not dropped). **RentFaster** (~95%) and
  **Rentals.ca** (~30%, `node.contact.phoneNumber`) expose the number in their
  search feed, so it's captured at scrape time. **Zumper** almost never includes it
  in the feed — the number lives on the per-listing **detail page**, so it's
  resolved **lazily on demand**: the contact button for a Zumper listing reads
  "Show phone number" and, on click, hits `GET /api/listings/{id}/phone`, which
  fetches+parses the detail page and returns the rendered panel (or link-out) HTML
  to swap in. This keeps the bulk scrape fast and spends a request only on listings
  the user actually pursues (the *lazy on-click* choice over eager per-page /
  bulk-on-scrape). **`_extract_detail_phone` checks several locations** — the number
  is usually at `detail.entity.data.listing_agents[].phone`, sometimes the older
  `agents[]`, sometimes only `crm_phone` (at `data` level or on
  `detail.activeListings[]`). (Reading *only* `agents[]` was the bug behind "the
  button only sometimes works" — most listings use `listing_agents`, so they looked
  like "no number" and fell back to link-out.) **Caching is result-aware**
  (`fetch_listing_phone` returns `(fetched_ok, raw)`): a *definitive* outcome — a
  number, or a cleanly-parsed page with genuinely none — is cached forever in
  `contact_cache` (a miss as empty `phone`); a *transient* failure (timeout / a
  Cloudflare challenge with no preloaded state) is **retried** (3 attempts) and, if
  still failing, **not cached**, so a later click can succeed instead of being stuck
  on link-out forever (the second half of the "sometimes works" bug). **Kijiji** never exposes a number in
  its feed, so Kijiji listings always fall through to the "Contact on kijiji →"
  link-out. The Zumper "fetch the detail page" trick does **not** work for Kijiji: its
  detail page only carries a **masked** number (`posterInfo.phoneNumber = "780809xxxx"`),
  and the real one comes from a reCAPTCHA-gated GraphQL call (`getProfilePhoneNumber`
  at `/anvil/api`, profileId = `posterInfo.posterId`). That call works headless up to
  the point of returning `"Recaptcha token is required"` for *every* poster type
  (individual `StandardProfileV2` and commercial `CommercialProfileV2` alike), so the
  number is unreachable without a real browser executing reCAPTCHA — out of scope for
  this $0/no-browser app. Hence: link-out only for Kijiji.
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
- **Saved searches + new-match counts.** The results page serializes the active
  `SearchFilters` (`filters_json`) into a `★ Save search` button that POSTs to
  `/api/searches`; `cache.add_saved_search` stores the JSON in `saved_searches` with a
  `last_viewed_at`. `GET /searches` lists them with both a **total match count** and a
  **new-since-last-viewed count**. Both apply the **full** filter set, commute included,
  via the shared `search.filter_and_enrich(filters, pool)` — the same filter→transit→
  filter core `run_search` uses — so the listed totals **equal** what opening the search
  shows. (Earlier this skipped the commute filter to avoid per-search geocoding, which
  made commute searches over-report and made two searches differing only by destination
  show an identical count; now they geocode + look up transit here too, cached so repeat
  loads stay fast.) `cache.count_listing_seen_after` counts the survivors first seen after
  `last_viewed_at` (reusing `listing_seen.first_seen`). Opening a search
  (`/searches/{id}/open`) runs it via the shared `_run_and_render` helper and
  `touch_saved_search` resets the count. Saving stamps `last_viewed_at = now`, so a
  fresh save shows 0 new (nothing is newer than the moment you saved). Email alerts
  (below) match the **same** way (`filter_and_enrich`), so an alert only fires for a
  listing that would actually appear when the search is opened — counts and alerts agree.
- **Saved-search email alerts.** Opt-in (`services/alerts.py`). Two independent
  halves: the **sending mailbox** (SMTP plumbing, `.env` `SMTP_*`, gated by
  `settings.smtp_configured` = host present) and the **recipient** (set in-app on the
  **Settings page**, stored in the `meta` k/v table under `cache.ALERT_EMAIL_KEY`;
  `.env` `ALERT_EMAIL_TO` is only a fallback). `alerts.resolve_recipient()` returns the
  DB value or that fallback; `dispatch_alerts` no-ops unless both SMTP is configured and
  a recipient resolves. (Recipient-in-DB so it can be changed without editing `.env` or
  restarting — single-user app, so there's exactly one global recipient, not per-user;
  real multi-user would need accounts.) Alerts fire from **one**
  dispatch point: after a *fresh scrape*, `run_search` calls `dispatch_alerts(pool)`,
  which for each saved search matches the pool with `search.filter_and_enrich` (the
  **full** filter, commute included — so an alert never fires for a listing outside the
  search's travel-time limit) and emails the subset first seen since that search's
  `last_alerted_at` (`cache.list_listing_seen_after`), then `mark_search_alerted`
  stamps now so they never re-notify. `last_alerted_at` is a **separate** column from
  `last_viewed_at` so emailing and in-app viewing don't interfere (opening a search
  resets the badge but not the alert baseline, and vice-versa); the migration backfills
  it to `last_viewed_at`, and new saves seed it to `now` (no blast on first save).
  Email is plain-text via stdlib `smtplib` + STARTTLS, sent in a thread
  (`asyncio.to_thread`), **no new dependency**. Dispatch failures are caught/logged,
  never raised — a bad SMTP config can't break a search. Because the pool only
  refreshes on a user search, a background **`alert_poller`** (started in `main.py`
  lifespan when `ALERT_POLL_MINUTES > 0`) forces a `run_search(SearchFilters())` on
  that cadence so alerts arrive while the app is idle — dispatch still happens inside
  `run_search`, so the poller is just a periodic trigger (set the interval ≥ the cache
  TTL or it mostly hits cache and finds nothing new). No emails are ever sent unless a
  saved search has genuinely new matches.
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
# optional saved-search email alerts (commented examples in .env):
SMTP_HOST=  SMTP_PORT=587  SMTP_USER=  SMTP_PASSWORD=  SMTP_FROM=
ALERT_EMAIL_TO=  ALERT_POLL_MINUTES=0
```
Update the key by editing `.env` directly (don't paste secrets into chat). The
running app must be restarted (or it auto-reloads) to pick up a new key. Alerts
stay off until `SMTP_HOST` + `ALERT_EMAIL_TO` are set (Gmail: App Password +
`smtp.gmail.com:587`); set `ALERT_POLL_MINUTES` > 0 to auto-check while idle.

## Git / pushing

- Remote: `origin` = `git@github-personal:Betts6430/ApartmentFinder.git`
  (personal GitHub account **Betts6430**).
- Pushes use a **dedicated SSH key via host alias** so they don't collide with the
  user's org GitHub identity (`ualberta-baymax`):
  - `~/.ssh/config` has `Host github-personal` → `IdentityFile ~/.ssh/id_ed25519_personal`
  - `ssh -T git@github-personal` should greet **Betts6430**.
- `git push origin main` just works from this repo. Default branch is `main`.
- `.gitignore` excludes `.env`, `.venv/`, `data/`, `.claude/settings.local.json`.

## Status (as of 2026-06-26)

Working: all four scrapers (RentFaster, Rentals.ca, Zumper, Kijiji), pool caching, ranking, blank-field-tolerant search,
commute filter (geocode + Distance Matrix), location autocomplete, cross-source
dedupe, paginated results with Grid/List/Map views, the contact button, saved-listing
favorites, "New" listing badges, address normalization, price-drop tracking,
saved searches with new-match counts, and opt-in saved-search email alerts.

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
`price_history`); **saved searches** (named `SearchFilters` snapshots) with
**new-match counts** since last viewed; **input hardening** (unknown `sort_by` /
`property_types` no longer 500 — they degrade; saved-search `filters_json` is
script-safe-escaped); **Kijiji scraper** (`__NEXT_DATA__` Apollo cache, ~1700 Edmonton
listings, cents/×10-bath decode, totalCount-driven pagination, link-out contact; v7 pool);
**Maps JavaScript API** enabled (the Map view's separate API — gray-box symptom when off);
**saved-search email alerts** (opt-in SMTP; `services/alerts.py` `dispatch_alerts` after a
fresh scrape, `last_alerted_at` baseline, background `alert_poller`); **Settings page**
(`/settings`) to set the alert recipient email in-app (DB-stored, `.env` fallback) +
a **"Send test email"** button (`/api/settings/test-email` → `alerts.send_test_email`).

### Possible next steps (not started)
- Cache `/api/places/autocomplete` responses (currently every keystroke-after-debounce
  hits Google; cheap but not free).
- More scrapers: Places4Students (U of A–relevant), liv.rent/RentSeeker/4rent (breadth).
  Apartments.com / Zillow stay deferred (Cloudflare-hard; Zillow has ~no Canadian
  rentals). Facebook Marketplace skipped (login + account-ban risk).
- ~~Email alerts for saved-search new matches~~ — **done** (opt-in SMTP, see above).
  Still open: push/desktop notifications, and an HTML (vs plain-text) email template.
- Consider Routes API migration only if legacy Distance Matrix gets sunset (mind the
  transit caveat above).
```
