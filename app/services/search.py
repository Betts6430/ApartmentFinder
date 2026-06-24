from __future__ import annotations

import asyncio
import logging
import re

from app import cache
from app.models import Listing, SearchFilters
from app.scrapers import SCRAPERS
from app.services import ranking, transit

log = logging.getLogger(__name__)


def _norm_address(addr: str) -> str:
    """Loosely normalize a street address so the same place written differently
    across sites collapses to one string (drops unit/punctuation, abbreviates,
    strips directionals)."""
    a = addr.lower()
    a = re.sub(r"[#.,]", " ", a)
    a = re.sub(r"\bunit\b|\bsuite\b|\bapt\b|\bapartment\b", " ", a)
    a = re.sub(r"\bstreet\b", "st", a)
    a = re.sub(r"\bavenue\b", "ave", a)
    a = re.sub(r"\bdrive\b", "dr", a)
    a = re.sub(r"\bboulevard\b", "blvd", a)
    a = re.sub(r"\broad\b", "rd", a)
    a = re.sub(r"\b(nw|ne|sw|se)\b", " ", a)  # quadrant directionals
    a = re.sub(r"\s+", " ", a).strip()
    return a


def _dedupe_key(l: Listing) -> tuple | None:
    """A signature identifying the same physical posting across sites: price +
    bedrooms + bathrooms at the same location. Coordinates (rounded to ~100m) are
    the location signal when present, falling back to a normalized address.
    Returns None when there's not enough location info to dedupe safely."""
    if l.lat is not None and l.lng is not None:
        loc: object = (round(l.lat, 3), round(l.lng, 3))
    elif l.address:
        loc = _norm_address(l.address)
        if not loc:
            return None
    else:
        return None
    return (round(l.price), l.bedrooms, l.bathrooms, loc)


def _completeness(l: Listing) -> tuple:
    """How rich a listing is — used to pick which copy of a duplicate to keep."""
    return (
        len(l.photos),
        1 if (l.lat is not None and l.lng is not None) else 0,
        1 if l.address else 0,
        len(l.description),
        len(l.amenities),
    )


def _dedupe_cross_source(listings: list[Listing]) -> list[Listing]:
    """Collapse the same posting appearing on multiple sites into one entry,
    keeping the copy with the most complete data. Listings without enough info
    to key on are kept as-is. Insertion order is preserved."""
    best: dict[tuple, Listing] = {}
    extras: list[Listing] = []
    for l in listings:
        key = _dedupe_key(l)
        if key is None:
            extras.append(l)
            continue
        cur = best.get(key)
        if cur is None or _completeness(l) > _completeness(cur):
            best[key] = l
    return list(best.values()) + extras


async def _scrape_all(filters: SearchFilters) -> list[Listing]:
    """Run every registered scraper in parallel, dedupe by listing id, then
    collapse cross-site duplicates of the same posting."""
    results = await asyncio.gather(
        *(s.scrape(filters) for s in SCRAPERS), return_exceptions=True
    )
    listings: list[Listing] = []
    for scraper, result in zip(SCRAPERS, results):
        if isinstance(result, Exception):
            log.exception("scraper %s failed", scraper.name, exc_info=result)
            continue
        listings.extend(result)
    seen: dict[str, Listing] = {}
    for l in listings:
        seen.setdefault(l.id, l)
    unique = list(seen.values())
    deduped = _dedupe_cross_source(unique)
    if len(deduped) < len(unique):
        log.info("cross-source dedupe: %d → %d listings", len(unique), len(deduped))
    return deduped


async def run_search(filters: SearchFilters) -> list[Listing]:
    """Top-level search orchestration: cache → scrape → filter → transit enrich → final filter."""
    key = filters.scrape_cache_key()
    listings = await cache.get_cached_search(key)
    if listings is None:
        log.info("scrape-pool cache miss — running %d scrapers", len(SCRAPERS))
        listings = await _scrape_all(filters)
        if listings:
            await cache.save_search(key, listings)
    else:
        log.info("scrape-pool cache hit (%d listings)", len(listings))

    # Apply non-transit filters first so we only pay for transit enrichment on survivors.
    survivors = [l for l in listings if l.matches(filters, include_transit=False)]

    if filters.transit_target:
        dest = await transit.geocode(filters.transit_target)
        if dest is None:
            log.warning("could not geocode transit target %r — skipping commute filter", filters.transit_target)
        else:
            geo_survivors = [l for l in survivors if l.lat is not None and l.lng is not None]
            if len(geo_survivors) < len(survivors):
                log.debug(
                    "commute filter: %d of %d survivors lack coordinates and can't be scored",
                    len(survivors) - len(geo_survivors), len(survivors),
                )
            origins = [(l.lat, l.lng) for l in geo_survivors]  # type: ignore[arg-type]
            minutes = await transit.compute_transit(origins, dest, filters.transit_mode)
            for l, m in zip(geo_survivors, minutes):
                l.transit_minutes = m
            # Apply transit_minutes_max now that times are populated.
            if filters.transit_minutes_max is not None:
                survivors = [
                    l for l in survivors
                    if l.transit_minutes is not None
                    and l.transit_minutes <= filters.transit_minutes_max
                ]

    ranking.apply_scores(survivors, filters)
    return ranking.sort_listings(survivors, filters.sort_by)
