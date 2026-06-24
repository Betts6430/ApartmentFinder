"""SQLite-backed cache.

Three caches:
- listings: id -> full Listing JSON (kept ~24h to allow cross-search reuse)
- search_cache: filter_hash -> list of listing IDs (TTL = SEARCH_CACHE_TTL_HOURS)
- transit_cache: (origin_lat,origin_lng,dest_lat,dest_lng,mode) -> minutes (no TTL)
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Optional

import aiosqlite

from app.config import settings
from app.models import Listing

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

CREATE INDEX IF NOT EXISTS idx_listings_scraped_at ON listings(scraped_at);
CREATE INDEX IF NOT EXISTS idx_search_cache_created_at ON search_cache(created_at);
"""


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(_SCHEMA)
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
    cutoff = datetime.utcnow() - timedelta(hours=settings.search_cache_ttl_hours)
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
            (filter_hash, json.dumps([l.id for l in listings]), datetime.utcnow().isoformat()),
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
    now = datetime.utcnow().isoformat()
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
            (qn, lat, lng, formatted, datetime.utcnow().isoformat()),
        )
        await db.commit()
