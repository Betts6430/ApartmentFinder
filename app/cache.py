"""SQLite-backed cache.

Tables (see `_SCHEMA`):
- listings:      id -> full Listing JSON (the scraped pool; pruned by age via `prune`)
- search_cache:  filter_hash -> listing IDs (logical TTL = SEARCH_CACHE_TTL_HOURS)
- transit_cache: (o_lat,o_lng,d_lat,d_lng,mode) -> minutes (no TTL — commutes are stable)
- geocode_cache / contact_cache: location & phone lookups (no TTL — they don't change)
- autocomplete_cache: query prefix -> Places suggestions (TTL-pruned; suggestions can drift)
- favorites:     saved-listing snapshots (kept until the user un-saves)
- listing_seen / price_history / meta / saved_searches: new-badge, drop-tracking,
  key/value, and saved-search state.

`prune()` (run at startup) drops aged-out rows so the file doesn't grow forever;
the no-TTL caches above are intentionally left alone.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Optional

import aiosqlite

from app.config import settings
from app.models import Listing, SearchFilters
from app.timeutil import utcnow

DB_PATH = settings.cache_dir / "cache.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    payload TEXT NOT NULL,
    scraped_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS search_cache (
    filter_hash TEXT PRIMARY KEY,
    listing_ids TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transit_cache (
    route_hash TEXT PRIMARY KEY,
    minutes REAL NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS geocode_cache (
    query_norm TEXT PRIMARY KEY,
    lat REAL NOT NULL,
    lng REAL NOT NULL,
    formatted TEXT,
    created_at TEXT NOT NULL
);

-- Places-autocomplete suggestions, keyed by the normalized query prefix, so repeat
-- type-aheads of the same text don't re-bill Google. TTL-checked on read and pruned
-- (unlike geocode, a prefix's suggestions can drift as places open/close).
CREATE TABLE IF NOT EXISTS autocomplete_cache (
    query_norm TEXT PRIMARY KEY,
    suggestions TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Saved listings. Stores a full snapshot so a favorite survives the pool cache
-- expiring or the listing being delisted at the source.
CREATE TABLE IF NOT EXISTS favorites (
    id TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    favorited_at TEXT NOT NULL
);

-- First time each listing id was ever seen, for "new listing" badges.
CREATE TABLE IF NOT EXISTS listing_seen (
    id TEXT PRIMARY KEY,
    first_seen TEXT NOT NULL
);

-- Small key/value store (scrape boundaries for new-listing detection, etc.).
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Lazily-resolved contact numbers (e.g. Zumper detail-page lookups), keyed by
-- listing id. Never expires — phone numbers don't change. A row with an empty
-- `phone` records a "looked up, none found" miss so we don't re-fetch.
CREATE TABLE IF NOT EXISTS contact_cache (
    id TEXT PRIMARY KEY,
    phone TEXT NOT NULL,
    phone_ext TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);

-- Per-listing price history for price-drop detection. One row per *change* (not
-- per scrape), so it stays compact. Used to flag drops and the "Price drops" sort.
CREATE TABLE IF NOT EXISTS price_history (
    id TEXT NOT NULL,
    price REAL NOT NULL,
    seen_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_price_history_id ON price_history(id);

-- Saved searches: a named SearchFilters snapshot. `last_viewed_at` anchors the
-- "new matches since you last looked" count.
CREATE TABLE IF NOT EXISTS saved_searches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    filters TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_viewed_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_listings_scraped_at ON listings(scraped_at);
CREATE INDEX IF NOT EXISTS idx_search_cache_created_at ON search_cache(created_at);
"""


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        # WAL allows concurrent reads during a write and survives the many short-lived
        # connections this module opens; a one-time pragma persisted on the DB file.
        await db.execute("PRAGMA journal_mode=WAL")
        await db.executescript(_SCHEMA)
        # Migration: `last_alerted_at` anchors email alerts independently of viewing.
        # Backfill existing rows to last_viewed_at so old searches don't blast an alert
        # for every listing already in the pool the first time the poller runs.
        async with db.execute("PRAGMA table_info(saved_searches)") as cur:
            cols = {r[1] for r in await cur.fetchall()}
        if "last_alerted_at" not in cols:
            await db.execute("ALTER TABLE saved_searches ADD COLUMN last_alerted_at TEXT")
            await db.execute(
                "UPDATE saved_searches SET last_alerted_at = last_viewed_at WHERE last_alerted_at IS NULL"
            )
        await db.commit()


async def prune(
    listings_days: int = 7, price_history_days: int = 90, seen_days: int = 120
) -> None:
    """Drop aged-out rows so the DB file doesn't grow without bound.

    Safe because: pruned `listings` are far older than the pool's few-hour TTL (so a
    live cached pool is never touched), and favorites are snapshotted in their own
    table. The no-TTL caches (transit/geocode/contact) are intentionally never pruned.
    """
    now = utcnow()
    l_cut = (now - timedelta(days=listings_days)).isoformat()
    ph_cut = (now - timedelta(days=price_history_days)).isoformat()
    seen_cut = (now - timedelta(days=seen_days)).isoformat()
    sc_cut = (now - timedelta(hours=settings.search_cache_ttl_hours)).isoformat()
    ac_cut = (now - timedelta(days=AUTOCOMPLETE_TTL_DAYS)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM listings WHERE scraped_at < ?", (l_cut,))
        await db.execute("DELETE FROM price_history WHERE seen_at < ?", (ph_cut,))
        await db.execute("DELETE FROM listing_seen WHERE first_seen < ?", (seen_cut,))
        await db.execute("DELETE FROM search_cache WHERE created_at < ?", (sc_cut,))
        await db.execute("DELETE FROM autocomplete_cache WHERE created_at < ?", (ac_cut,))
        await db.commit()


async def save_listings(listings: list[Listing]) -> None:
    if not listings:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executemany(
            "INSERT OR REPLACE INTO listings (id, source, payload, scraped_at) VALUES (?, ?, ?, ?)",
            [
                (l.id, l.source, l.model_dump_json(), l.scraped_at.isoformat())
                for l in listings
            ],
        )
        await db.commit()


async def get_listings(ids: list[str]) -> list[Listing]:
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            f"SELECT payload FROM listings WHERE id IN ({placeholders})", ids
        ) as cursor:
            rows = await cursor.fetchall()
    return [Listing.model_validate_json(r[0]) for r in rows]


async def get_cached_search(filter_hash: str) -> Optional[list[Listing]]:
    cutoff = utcnow() - timedelta(hours=settings.search_cache_ttl_hours)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT listing_ids, created_at FROM search_cache WHERE filter_hash = ?",
            (filter_hash,),
        ) as cursor:
            row = await cursor.fetchone()
    if row is None:
        return None
    created = datetime.fromisoformat(row[1])
    if created < cutoff:
        return None
    ids = json.loads(row[0])
    return await get_listings(ids)


async def save_search(filter_hash: str, listings: list[Listing]) -> None:
    await save_listings(listings)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO search_cache (filter_hash, listing_ids, created_at) VALUES (?, ?, ?)",
            (filter_hash, json.dumps([l.id for l in listings]), utcnow().isoformat()),
        )
        await db.commit()


def _route_hash(o_lat: float, o_lng: float, d_lat: float, d_lng: float, mode: str) -> str:
    import hashlib

    key = f"{round(o_lat, 5)}|{round(o_lng, 5)}|{round(d_lat, 5)}|{round(d_lng, 5)}|{mode}"
    return hashlib.sha256(key.encode()).hexdigest()


async def get_cached_transit_many(
    routes: list[tuple[float, float, float, float, str]],
) -> list[Optional[float]]:
    """Look up many routes in a single query. Returns a list aligned to `routes`,
    with None where a route isn't cached. routes = (o_lat, o_lng, d_lat, d_lng, mode)."""
    if not routes:
        return []
    hashes = [_route_hash(*r) for r in routes]
    placeholders = ",".join("?" * len(hashes))
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            f"SELECT route_hash, minutes FROM transit_cache WHERE route_hash IN ({placeholders})",
            hashes,
        ) as cursor:
            rows = await cursor.fetchall()
    found = {h: m for h, m in rows}
    return [found.get(h) for h in hashes]


async def save_transit_bulk(
    rows: list[tuple[float, float, float, float, str, float]],
) -> None:
    """rows = (o_lat, o_lng, d_lat, d_lng, mode, minutes)"""
    if not rows:
        return
    now = utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executemany(
            "INSERT OR REPLACE INTO transit_cache (route_hash, minutes, created_at) VALUES (?, ?, ?)",
            [(_route_hash(*r[:5]), r[5], now) for r in rows],
        )
        await db.commit()


def _norm_query(q: str) -> str:
    return " ".join(q.strip().lower().split())


async def get_cached_geocode(query: str) -> Optional[tuple[float, float]]:
    qn = _norm_query(query)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT lat, lng FROM geocode_cache WHERE query_norm = ?", (qn,)
        ) as cursor:
            row = await cursor.fetchone()
    return (row[0], row[1]) if row else None


async def save_geocode(query: str, lat: float, lng: float, formatted: str = "") -> None:
    qn = _norm_query(query)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO geocode_cache (query_norm, lat, lng, formatted, created_at) VALUES (?, ?, ?, ?, ?)",
            (qn, lat, lng, formatted, utcnow().isoformat()),
        )
        await db.commit()


# Autocomplete suggestions go stale slowly; a month between refreshes keeps Google
# calls down while still picking up new places eventually. Kept in sync with the
# prune window so a row isn't deleted while still considered fresh on read.
AUTOCOMPLETE_TTL_DAYS = 30


async def get_cached_autocomplete(query: str) -> Optional[list[str]]:
    """Cached Places-autocomplete suggestions for a query prefix, or None when absent
    or older than AUTOCOMPLETE_TTL_DAYS. Keyed by the same normalization as geocode."""
    qn = _norm_query(query)
    cutoff = (utcnow() - timedelta(days=AUTOCOMPLETE_TTL_DAYS)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT suggestions, created_at FROM autocomplete_cache WHERE query_norm = ?", (qn,)
        ) as cursor:
            row = await cursor.fetchone()
    if row is None or row[1] < cutoff:
        return None
    try:
        return json.loads(row[0])
    except json.JSONDecodeError:
        return None


async def save_autocomplete(query: str, suggestions: list[str]) -> None:
    """Cache a *successful* autocomplete response (an empty list is a valid result —
    Google found no matches). Callers must NOT call this on a failed/blocked request,
    or a transient error would be cached as a permanent empty result."""
    qn = _norm_query(query)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO autocomplete_cache (query_norm, suggestions, created_at) VALUES (?, ?, ?)",
            (qn, json.dumps(suggestions), utcnow().isoformat()),
        )
        await db.commit()


# --- Favorites (saved listings) ---------------------------------------------

async def add_favorite(listing: Listing) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO favorites (id, payload, favorited_at) VALUES (?, ?, ?)",
            (listing.id, listing.model_dump_json(), utcnow().isoformat()),
        )
        await db.commit()


async def remove_favorite(listing_id: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM favorites WHERE id = ?", (listing_id,))
        await db.commit()


async def get_favorite_ids() -> set[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id FROM favorites") as cursor:
            rows = await cursor.fetchall()
    return {r[0] for r in rows}


async def is_favorite(listing_id: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM favorites WHERE id = ?", (listing_id,)) as cursor:
            return await cursor.fetchone() is not None


async def get_favorites() -> list[Listing]:
    """Saved listings, most-recently-favorited first."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT payload FROM favorites ORDER BY favorited_at DESC"
        ) as cursor:
            rows = await cursor.fetchall()
    return [Listing.model_validate_json(r[0]) for r in rows]


# --- New-listing tracking ----------------------------------------------------

async def get_meta(key: str) -> Optional[str]:
    """Read a value from the small key/value store (None if unset)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT value FROM meta WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
    return row[0] if row else None


async def set_meta(key: str, value: str) -> None:
    """Write a value into the small key/value store."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value)
        )
        await db.commit()


# Key under which the saved-search alert recipient is stored (set via the Settings
# page; overrides the .env ALERT_EMAIL_TO fallback). See services/alerts.py.
ALERT_EMAIL_KEY = "alert_email_to"


async def record_scrape(ids: list[str]) -> None:
    """Record a fresh scrape: stamp first-seen for any new ids, and advance the
    scrape boundary so the just-appeared listings can be flagged as "new". The
    boundary (`prev_scrape_at`) is the *previous* scrape time; nothing is flagged
    on the very first scrape, avoiding a cold-start where everything looks new."""
    now = utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT value FROM meta WHERE key = 'last_scrape_at'") as cur:
            row = await cur.fetchone()
        prev = row[0] if row else ""
        await db.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('prev_scrape_at', ?)", (prev,)
        )
        if ids:
            await db.executemany(
                "INSERT OR IGNORE INTO listing_seen (id, first_seen) VALUES (?, ?)",
                [(i, now) for i in ids],
            )
        await db.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('last_scrape_at', ?)", (now,)
        )
        await db.commit()


async def get_first_seen_many(ids: list[str]) -> dict[str, str]:
    """Map each given id to its first-seen ISO timestamp (omitted if never seen).
    Lets the "Newest" sort rank by genuine listing recency rather than the
    near-uniform scrape time."""
    if not ids:
        return {}
    out: dict[str, str] = {}
    async with aiosqlite.connect(DB_PATH) as db:
        for i in range(0, len(ids), _SQLITE_MAX_VARS):
            chunk = ids[i : i + _SQLITE_MAX_VARS]
            ph = ",".join("?" * len(chunk))
            async with db.execute(
                f"SELECT id, first_seen FROM listing_seen WHERE id IN ({ph})", chunk
            ) as cur:
                out.update({r[0]: r[1] for r in await cur.fetchall()})
    return out


async def get_new_ids(ids: list[str]) -> set[str]:
    """Of the given ids, which first appeared since the previous scrape."""
    if not ids:
        return set()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT value FROM meta WHERE key = 'prev_scrape_at'") as cur:
            row = await cur.fetchone()
        prev = row[0] if row else ""
        if not prev:
            return set()
        placeholders = ",".join("?" * len(ids))
        async with db.execute(
            f"SELECT id FROM listing_seen WHERE id IN ({placeholders}) AND first_seen > ?",
            [*ids, prev],
        ) as cursor:
            rows = await cursor.fetchall()
    return {r[0] for r in rows}


# --- Lazily-resolved contact numbers ----------------------------------------

async def get_contact(listing_id: str) -> Optional[tuple[Optional[str], Optional[str]]]:
    """Return a cached (phone, ext) for a listing, or None if never looked up.
    A looked-up miss is returned as (None, None) so callers skip a re-fetch."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT phone, phone_ext FROM contact_cache WHERE id = ?", (listing_id,)
        ) as cursor:
            row = await cursor.fetchone()
    if row is None:
        return None
    return (row[0] or None, row[1] or None)


async def save_contact(listing_id: str, phone: Optional[str], phone_ext: Optional[str]) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO contact_cache (id, phone, phone_ext, fetched_at) VALUES (?, ?, ?, ?)",
            (listing_id, phone or "", phone_ext or "", utcnow().isoformat()),
        )
        await db.commit()


# --- Price history -----------------------------------------------------------

# SQLite caps a statement at ~999 bound parameters; chunk IN (...) queries below it.
_SQLITE_MAX_VARS = 900


async def record_prices(items: list[Listing]) -> None:
    """Append a price point for any listing whose price changed since its last
    recorded one (or that has no history yet). Called on each fresh scrape, so the
    history stays compact — one row per actual change, not one per scrape."""
    if not items:
        return
    now = utcnow().isoformat()
    ids = [l.id for l in items]
    latest: dict[str, float] = {}
    async with aiosqlite.connect(DB_PATH) as db:
        for i in range(0, len(ids), _SQLITE_MAX_VARS):
            chunk = ids[i : i + _SQLITE_MAX_VARS]
            ph = ",".join("?" * len(chunk))
            async with db.execute(
                f"SELECT id, price, MAX(seen_at) FROM price_history WHERE id IN ({ph}) GROUP BY id",
                chunk,
            ) as cur:
                for r in await cur.fetchall():
                    latest[r[0]] = r[1]
        to_insert = [
            (l.id, float(l.price), now)
            for l in items
            if l.id not in latest or abs(latest[l.id] - float(l.price)) >= 1.0
        ]
        if to_insert:
            await db.executemany(
                "INSERT INTO price_history (id, price, seen_at) VALUES (?, ?, ?)", to_insert
            )
            await db.commit()


async def get_price_drops(ids: list[str], within_days: int = 30) -> dict[str, float]:
    """For the given ids, return id -> previous price for listings whose most recent
    price change was a *drop* within `within_days`. Returns only the prior price; the
    caller already has the current one and can show the delta."""
    if not ids:
        return {}
    cutoff = (utcnow() - timedelta(days=within_days)).isoformat()
    out: dict[str, float] = {}
    async with aiosqlite.connect(DB_PATH) as db:
        for i in range(0, len(ids), _SQLITE_MAX_VARS):
            chunk = ids[i : i + _SQLITE_MAX_VARS]
            ph = ",".join("?" * len(chunk))
            async with db.execute(
                f"SELECT id, price, seen_at FROM price_history WHERE id IN ({ph}) "
                "ORDER BY id, seen_at DESC",
                chunk,
            ) as cur:
                rows = await cur.fetchall()
            # Rows come grouped by id, newest first. Compare the two most recent points.
            j, n = 0, len(rows)
            while j < n:
                lid = rows[j][0]
                k = j
                while k < n and rows[k][0] == lid:
                    k += 1
                pts = rows[j:k]
                j = k
                if len(pts) >= 2:
                    cur_price, cur_seen, prev_price = pts[0][1], pts[0][2], pts[1][1]
                    if cur_seen >= cutoff and prev_price > cur_price:
                        out[lid] = prev_price
    return out


# --- Saved searches ----------------------------------------------------------

async def add_saved_search(name: str, filters: SearchFilters) -> int:
    now = utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO saved_searches (name, filters, created_at, last_viewed_at, last_alerted_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (name, filters.model_dump_json(), now, now, now),
        )
        await db.commit()
        return cur.lastrowid


def _row_to_saved(r) -> dict:
    return {
        "id": r[0],
        "name": r[1],
        "filters": SearchFilters.model_validate_json(r[2]),
        "created_at": r[3],
        "last_viewed_at": r[4],
        "last_alerted_at": r[5],
    }


_SAVED_COLS = "id, name, filters, created_at, last_viewed_at, last_alerted_at"


async def get_saved_searches() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            f"SELECT {_SAVED_COLS} FROM saved_searches ORDER BY created_at DESC"
        ) as cur:
            rows = await cur.fetchall()
    return [_row_to_saved(r) for r in rows]


async def get_saved_search(search_id: int) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            f"SELECT {_SAVED_COLS} FROM saved_searches WHERE id = ?",
            (search_id,),
        ) as cur:
            row = await cur.fetchone()
    return _row_to_saved(row) if row else None


async def mark_search_alerted(search_id: int) -> None:
    """Stamp a saved search as just-alerted, so its matches won't re-notify."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE saved_searches SET last_alerted_at = ? WHERE id = ?",
            (utcnow().isoformat(), search_id),
        )
        await db.commit()


async def delete_saved_search(search_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM saved_searches WHERE id = ?", (search_id,))
        await db.commit()


async def touch_saved_search(search_id: int) -> None:
    """Mark a saved search as just-viewed, resetting its new-match count."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE saved_searches SET last_viewed_at = ? WHERE id = ?",
            (utcnow().isoformat(), search_id),
        )
        await db.commit()


async def count_listing_seen_after(ids: list[str], since: str) -> int:
    """How many of the given listing ids were first seen strictly after `since` —
    i.e. new matches since a saved search was last viewed."""
    if not ids:
        return 0
    total = 0
    async with aiosqlite.connect(DB_PATH) as db:
        for i in range(0, len(ids), _SQLITE_MAX_VARS):
            chunk = ids[i : i + _SQLITE_MAX_VARS]
            ph = ",".join("?" * len(chunk))
            async with db.execute(
                f"SELECT COUNT(*) FROM listing_seen WHERE id IN ({ph}) AND first_seen > ?",
                [*chunk, since],
            ) as cur:
                total += (await cur.fetchone())[0]
    return total


async def list_listing_seen_after(ids: list[str], since: str) -> set[str]:
    """Which of the given listing ids were first seen strictly after `since` —
    the new-match subset (for alert emails), not just a count."""
    if not ids:
        return set()
    out: set[str] = set()
    async with aiosqlite.connect(DB_PATH) as db:
        for i in range(0, len(ids), _SQLITE_MAX_VARS):
            chunk = ids[i : i + _SQLITE_MAX_VARS]
            ph = ",".join("?" * len(chunk))
            async with db.execute(
                f"SELECT id FROM listing_seen WHERE id IN ({ph}) AND first_seen > ?",
                [*chunk, since],
            ) as cur:
                out.update(r[0] for r in await cur.fetchall())
    return out
