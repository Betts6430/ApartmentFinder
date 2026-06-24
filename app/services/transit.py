"""Google Maps integration: geocoding + Distance Matrix.

- geocode(query): "University of Alberta" -> (lat, lng), cached forever
- compute_transit(origins, dest, mode): bulk transit/driving/walking/cycling
  minutes per origin, batched 25 origins per Distance Matrix call,
  cached forever per (origin, dest, mode) tuple.

Edmonton listings + a single user target means each request hits at most ~25
distinct destination pairs per batch. We pin departure_time="now" for transit;
absolute precision isn't important and it makes results cacheable.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import httpx

from app import cache
from app.config import settings

log = logging.getLogger(__name__)

GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
DISTANCE_MATRIX_URL = "https://maps.googleapis.com/maps/api/distancematrix/json"

# Distance Matrix accepts up to 25 origins per request; we use 25 to maximize batching.
BATCH_SIZE = 25

# Bias geocoding toward Edmonton so "Whyte Ave" resolves locally.
EDMONTON_BIAS = "53.5461,-113.4938"  # downtown lat/lng


class TransitError(Exception):
    pass


async def geocode(query: str) -> Optional[tuple[float, float]]:
    """Resolve a free-form location query to (lat, lng) within Edmonton."""
    if not query or not query.strip():
        return None
    if not settings.google_maps_api_key:
        log.warning("geocode skipped: GOOGLE_MAPS_API_KEY not set")
        return None

    cached = await cache.get_cached_geocode(query)
    if cached is not None:
        return cached

    params = {
        "address": query,
        "key": settings.google_maps_api_key,
        "region": "ca",
        "components": "administrative_area:AB|country:CA",
        "bounds": "53.396,-113.715|53.712,-113.270",  # Edmonton city bounding box
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(GEOCODE_URL, params=params)
    if r.status_code != 200:
        log.warning("geocode HTTP %s: %s", r.status_code, r.text[:200])
        return None
    data = r.json()
    status = data.get("status")
    if status != "OK" or not data.get("results"):
        log.warning("geocode status=%s for %r: %s", status, query, data.get("error_message"))
        return None
    loc = data["results"][0]["geometry"]["location"]
    formatted = data["results"][0].get("formatted_address", "")
    lat, lng = loc["lat"], loc["lng"]
    await cache.save_geocode(query, lat, lng, formatted)
    return lat, lng


async def _distance_matrix_call(
    client: httpx.AsyncClient,
    origins: list[tuple[float, float]],
    dest: tuple[float, float],
    mode: str,
) -> list[Optional[float]]:
    params = {
        "origins": "|".join(f"{lat},{lng}" for lat, lng in origins),
        "destinations": f"{dest[0]},{dest[1]}",
        "mode": mode,
        "key": settings.google_maps_api_key,
        "units": "metric",
    }
    if mode == "transit":
        # Required for transit; a fixed weekday peak gives stable, cache-friendly results.
        params["departure_time"] = "now"

    r = await client.get(DISTANCE_MATRIX_URL, params=params, timeout=20.0)
    if r.status_code != 200:
        raise TransitError(f"distance_matrix HTTP {r.status_code}: {r.text[:200]}")
    data = r.json()
    if data.get("status") != "OK":
        raise TransitError(f"distance_matrix status={data.get('status')}: {data.get('error_message')}")
    rows = data.get("rows") or []
    out: list[Optional[float]] = []
    for row in rows:
        elements = row.get("elements") or [{}]
        e = elements[0]
        if e.get("status") == "OK":
            seconds = e.get("duration", {}).get("value")
            out.append(seconds / 60.0 if seconds is not None else None)
        else:
            out.append(None)
    return out


async def compute_transit(
    origins: list[tuple[float, float]],
    dest: tuple[float, float],
    mode: str = "transit",
) -> list[Optional[float]]:
    """Compute transit minutes for each origin -> dest. Returns list aligned to origins.

    Uses the SQLite transit_cache aggressively — only origins missing a cached
    entry hit the Distance Matrix API.
    """
    if not origins:
        return []
    if not settings.google_maps_api_key:
        log.warning("compute_transit skipped: GOOGLE_MAPS_API_KEY not set")
        return [None] * len(origins)

    d_lat, d_lng = dest
    cached_minutes: list[Optional[float]] = []
    uncached_indices: list[int] = []
    uncached_origins: list[tuple[float, float]] = []

    for i, (lat, lng) in enumerate(origins):
        m = await cache.get_cached_transit(lat, lng, d_lat, d_lng, mode)
        cached_minutes.append(m)
        if m is None:
            uncached_indices.append(i)
            uncached_origins.append((lat, lng))

    if not uncached_origins:
        return cached_minutes

    log.info(
        "compute_transit: %d cached, %d to fetch (mode=%s)",
        len(origins) - len(uncached_origins),
        len(uncached_origins),
        mode,
    )

    async with httpx.AsyncClient() as client:
        # Batch in chunks of BATCH_SIZE origins.
        sem = asyncio.Semaphore(4)  # limit parallel DM calls

        async def fetch_batch(batch_start: int, batch: list[tuple[float, float]]):
            async with sem:
                try:
                    return await _distance_matrix_call(client, batch, dest, mode)
                except Exception as e:
                    log.warning("distance_matrix batch failed: %s", e)
                    return [None] * len(batch)

        batches: list[tuple[int, list[tuple[float, float]]]] = []
        for i in range(0, len(uncached_origins), BATCH_SIZE):
            batches.append((i, uncached_origins[i : i + BATCH_SIZE]))

        batch_results = await asyncio.gather(*(fetch_batch(s, b) for s, b in batches))

    # Stitch back into cached_minutes and persist successful lookups.
    rows_to_cache: list[tuple[float, float, float, float, str, float]] = []
    for (start, batch), minutes_batch in zip(batches, batch_results):
        for offset, m in enumerate(minutes_batch):
            global_idx = uncached_indices[start + offset]
            cached_minutes[global_idx] = m
            if m is not None:
                o_lat, o_lng = uncached_origins[start + offset]
                rows_to_cache.append((o_lat, o_lng, d_lat, d_lng, mode, m))

    await cache.save_transit_bulk(rows_to_cache)
    return cached_minutes
