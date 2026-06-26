from __future__ import annotations

import asyncio
import logging
import math
import re
from collections import defaultdict
from dataclasses import dataclass, field

from app import cache
from app.models import Listing, SearchFilters
from app.scrapers import SCRAPERS
from app.services import ranking, transit

log = logging.getLogger(__name__)


@dataclass
class SearchResult:
    """Outcome of a search: the ranked listings plus any user-facing warnings
    (e.g. a commute target that couldn't be located)."""

    listings: list[Listing]
    warnings: list[str] = field(default_factory=list)


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


# Same posting across sites rarely has identical coordinates — each source
# geocodes independently, so the same building can differ by ~100m. Merge listings
# within this radius (with matching price/beds/baths). Kept small so genuinely
# distinct units a few blocks apart (which can share price/beds/baths) don't merge.
_DUP_RADIUS_KM = 0.2


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _completeness(l: Listing) -> tuple:
    """How rich a listing is — used to pick which copy of a duplicate to keep.
    A contactable copy (has a phone) is preferred so dedupe doesn't drop the one
    the Contact button can act on."""
    return (
        1 if l.phone else 0,
        len(l.photos),
        1 if (l.lat is not None and l.lng is not None) else 0,
        1 if l.address else 0,
        len(l.description),
        len(l.amenities),
    )


def _same_place(a: Listing, b: Listing) -> bool:
    """Whether two same-price/beds/baths listings are the same physical posting.
    Uses coordinate proximity when both have coords (robust to the ~100m jitter
    between sources, unlike rounding to a grid); otherwise falls back to a matching
    normalized street address. Listings with neither are never treated as the same."""
    if None not in (a.lat, a.lng, b.lat, b.lng):
        return _haversine_km(a.lat, a.lng, b.lat, b.lng) <= _DUP_RADIUS_KM  # type: ignore[arg-type]
    aa = _norm_address(a.address) if a.address else ""
    bb = _norm_address(b.address) if b.address else ""
    return bool(aa) and aa == bb


def _dedupe_cross_source(listings: list[Listing]) -> list[Listing]:
    """Collapse the same posting appearing on multiple sites (or repeated within
    one) into a single entry, keeping the copy with the most complete data.

    Listings are bucketed by (price, bedrooms, bathrooms) and, within a bucket,
    greedily clustered by location (coordinate proximity, else matching address).
    Listings with no usable location signal are kept as-is."""
    buckets: dict[tuple, list[Listing]] = defaultdict(list)
    for l in listings:
        buckets[(round(l.price), l.bedrooms, l.bathrooms)].append(l)

    out: list[Listing] = []
    for group in buckets.values():
        clusters: list[list[Listing]] = []
        for l in group:
            for cl in clusters:
                if _same_place(l, cl[0]):
                    cl.append(l)
                    break
            else:
                clusters.append([l])
        for cl in clusters:
            out.append(max(cl, key=_completeness))
    return out


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


async def run_search(filters: SearchFilters) -> SearchResult:
    """Top-level search orchestration: cache → scrape → filter → transit enrich → final filter."""
    warnings: list[str] = []
    key = filters.scrape_cache_key()
    listings = await cache.get_cached_search(key)
    if listings is None:
        log.info("scrape-pool cache miss — running %d scrapers", len(SCRAPERS))
        listings = await _scrape_all(filters)
        if listings:
            await cache.save_search(key, listings)
            # Stamp first-seen / advance the scrape boundary for new-listing badges.
            await cache.record_scrape([l.id for l in listings])
    else:
        log.info("scrape-pool cache hit (%d listings)", len(listings))

    # Apply non-transit filters first so we only pay for transit enrichment on survivors.
    survivors = [l for l in listings if l.matches(filters, include_transit=False)]

    if filters.transit_target:
        dest = await transit.geocode(filters.transit_target)
        if dest is None:
            log.warning("could not geocode transit target %r — skipping commute filter", filters.transit_target)
            warnings.append(
                f"Couldn't find “{filters.transit_target}” near Edmonton, so the commute "
                "filter was skipped. Try a more specific address or place name."
            )
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
    return SearchResult(listings=ranking.sort_listings(survivors, filters.sort_by), warnings=warnings)
