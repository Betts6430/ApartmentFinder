from __future__ import annotations

import asyncio
import logging
import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from app import cache
from app.models import Listing, SearchFilters
from app.scrapers import SCRAPERS
from app.services import health, ranking, transit

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


async def _scrape_all(filters: SearchFilters) -> tuple[list[Listing], dict[str, int]]:
    """Run every registered scraper in parallel, dedupe by listing id, then collapse
    cross-site duplicates of the same posting. Also returns each source's raw listing
    count (before dedupe; 0 if it errored) for health tracking."""
    results = await asyncio.gather(
        *(s.scrape(filters) for s in SCRAPERS), return_exceptions=True
    )
    listings: list[Listing] = []
    counts: dict[str, int] = {}
    for scraper, result in zip(SCRAPERS, results):
        if isinstance(result, Exception):
            log.exception("scraper %s failed", scraper.name, exc_info=result)
            counts[scraper.name] = 0
            continue
        counts[scraper.name] = len(result)
        listings.extend(result)
    seen: dict[str, Listing] = {}
    for l in listings:
        seen.setdefault(l.id, l)
    unique = list(seen.values())
    deduped = _dedupe_cross_source(unique)
    if len(deduped) < len(unique):
        log.info("cross-source dedupe: %d → %d listings", len(unique), len(deduped))
    return deduped, counts


async def _warn_unhealthy_sources() -> None:
    """Log a warning for any source whose latest scrape collapsed versus its own recent
    norm — so silent breakage surfaces in the logs (also shown on the Settings page)."""
    histories = await cache.get_recent_scrape_counts()
    for item in health.health_report(histories, [s.name for s in SCRAPERS]):
        if item["status"] == health.DOWN:
            log.warning(
                "scraper health: %s looks DOWN — latest scrape returned %s (recent norm ~%s)",
                item["source"], item["latest"], item["baseline"],
            )


async def filter_and_enrich(
    filters: SearchFilters, pool: list[Listing], *, mutate: bool = True
) -> tuple[list[Listing], list[str]]:
    """Apply non-transit filters, then the commute filter (geocode + transit enrich),
    to an already-loaded pool. Returns (survivors, warnings).

    Shared by `run_search` and the saved-searches match count so the two always agree
    — the count would otherwise have to skip the commute filter and over-report.

    The filtering decision is made from a local transit map, never from the listing
    objects, so the function is safe to run concurrently over the same pool (the
    /searches page does). `mutate=True` additionally writes `transit_minutes` onto the
    matched listings (run_search needs that for ranking/display); pass `mutate=False`
    for the count path to avoid racing on shared pool objects.
    """
    warnings: list[str] = []
    # Apply non-transit filters first so we only pay for transit enrichment on survivors.
    survivors = [l for l in pool if l.matches(filters, include_transit=False)]

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
            mins_by_id: dict[str, Optional[float]] = {}
            for l, m in zip(geo_survivors, minutes):
                mins_by_id[l.id] = m
                if mutate:
                    l.transit_minutes = m
            # Filter from the local map (not l.transit_minutes) so concurrent callers
            # with different destinations can't read each other's writes.
            if filters.transit_minutes_max is not None:
                survivors = [
                    l for l in survivors
                    if mins_by_id.get(l.id) is not None
                    and mins_by_id[l.id] <= filters.transit_minutes_max  # type: ignore[operator]
                ]
    return survivors, warnings


async def run_search(filters: SearchFilters) -> SearchResult:
    """Top-level search orchestration: cache → scrape → filter → transit enrich → final filter."""
    key = filters.scrape_cache_key()
    listings = await cache.get_cached_search(key)
    if listings is None:
        log.info("scrape-pool cache miss — running %d scrapers", len(SCRAPERS))
        listings, source_counts = await _scrape_all(filters)
        # Record per-source counts + flag any source that collapsed vs its norm. Done
        # unconditionally (even an all-empty scrape) so total breakage is captured too.
        await cache.record_scrape_counts(source_counts)
        await _warn_unhealthy_sources()
        if listings:
            await cache.save_search(key, listings)
            # Stamp first-seen / advance the scrape boundary for new-listing badges,
            # and append any changed prices for drop detection.
            await cache.record_scrape([l.id for l in listings])
            await cache.record_prices(listings)
            # New pool → check saved searches and email any new matches. Lazy import
            # avoids a circular dependency (alerts.py's poller imports run_search).
            from app.services.alerts import dispatch_alerts
            await dispatch_alerts(listings)
    else:
        log.info("scrape-pool cache hit (%d listings)", len(listings))

    survivors, warnings = await filter_and_enrich(filters, listings or [])

    # Enrich with the most recent price drop (if any) for badges + the drops sort,
    # and first-seen times so the "Newest" sort ranks by real recency.
    survivor_ids = [l.id for l in survivors]
    drops = await cache.get_price_drops(survivor_ids)
    first_seen = await cache.get_first_seen_many(survivor_ids)
    for l in survivors:
        if l.id in drops:
            l.prev_price = drops[l.id]
        ts = first_seen.get(l.id)
        if ts:
            try:
                l.first_seen = datetime.fromisoformat(ts)
            except ValueError:
                pass

    ranking.apply_scores(survivors, filters)
    return SearchResult(listings=ranking.sort_listings(survivors, filters.sort_by), warnings=warnings)
