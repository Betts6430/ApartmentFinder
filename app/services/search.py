from __future__ import annotations

import asyncio
import logging

from app import cache
from app.models import Listing, SearchFilters
from app.scrapers import SCRAPERS
from app.services import ranking, transit

log = logging.getLogger(__name__)


async def _scrape_all(filters: SearchFilters) -> list[Listing]:
    """Run every registered scraper in parallel and dedupe by listing id."""
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
    return list(seen.values())


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
