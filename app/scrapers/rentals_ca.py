"""Rentals.ca scraper.

Rentals.ca embeds its search response as an inline `App.store.search = { response: {...} }`
JavaScript assignment on each search page. We fetch pages 1..N in parallel, extract the
inline JSON, and map each edge.node to our normalized Listing.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import re
from typing import Any, Optional

from curl_cffi import requests as cr  # type: ignore[import-not-found]

from app.models import Listing, PropertyType, SearchFilters
from app.scrapers.base import Scraper, normalize_phone, sane_sqft

log = logging.getLogger(__name__)

BASE_URL = "https://rentals.ca"
SEARCH_URL = f"{BASE_URL}/edmonton"
MAX_PAGES = 60  # 25/page * 60 = 1500 listings cap — Edmonton has ~1450 active

_ANCHOR_RE = re.compile(r"App\.store\.search\s*=\s*\{[\s\S]*?response:\s*(\{)")

_TYPE_TIER_MAP = {
    "apartment": PropertyType.APARTMENT,
    "condo": PropertyType.CONDO,
    "townhouse": PropertyType.TOWNHOUSE,
    "house": PropertyType.HOUSE,
    "duplex": PropertyType.HOUSE,
    "basement": PropertyType.BASEMENT,
    "room": PropertyType.ROOM,
    "loft": PropertyType.APARTMENT,
}


def _stable_id(source: str, source_id: Any) -> str:
    return hashlib.sha256(f"{source}:{source_id}".encode()).hexdigest()[:24]


def _extract_response_json(html: str) -> Optional[dict[str, Any]]:
    """Parse the inline `response: { ... }` JSON object from the page."""
    m = _ANCHOR_RE.search(html)
    if not m:
        return None
    start = m.end() - 1  # the `{`
    depth = 0
    in_str = False
    esc = False
    i = start
    while i < len(html):
        c = html[i]
        if esc:
            esc = False
        elif c == "\\":
            esc = True
        elif c == '"':
            in_str = not in_str
        elif not in_str:
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(html[start : i + 1])
                    except json.JSONDecodeError:
                        return None
        i += 1
    return None


def _map_property_type(listing_type: Optional[str]) -> PropertyType:
    if not listing_type:
        return PropertyType.OTHER
    parts = listing_type.split(":")
    # Middle tier is the property category (e.g. "apartment", "house")
    for tier in parts[1:]:
        if tier in _TYPE_TIER_MAP:
            return _TYPE_TIER_MAP[tier]
    return PropertyType.OTHER


def _pick_image(images: list[dict[str, Any]]) -> Optional[str]:
    if not images:
        return None
    scales = images[0].get("scales") or []
    # Prefer large > medium > small
    by_name = {s.get("name"): s.get("url") for s in scales if s.get("url")}
    return by_name.get("large") or by_name.get("medium") or by_name.get("small")


def _pets_from_options(opts: Optional[list[str]]) -> Optional[bool]:
    if not opts:
        return None
    s = {str(o).lower() for o in opts}
    if "none" in s or "no" in s:
        return False
    if s & {"all", "cats", "dogs", "yes"}:
        return True
    return None


def _parking_available(parking: Optional[dict[str, Any]]) -> Optional[bool]:
    if not parking:
        return None
    if parking.get("parkingAvailable") is True:
        return True
    if parking.get("parkingTypes"):
        return True
    return None


def _parse_node(node: dict[str, Any]) -> Optional[Listing]:
    try:
        nid = node.get("id")
        if not nid:
            return None

        rent_range = node.get("rentRange") or []
        price = rent_range[0] if rent_range else None
        if price is None or price <= 0:
            return None  # listings without published price

        beds_range = node.get("bedsRange") or [0.0]
        baths_range = node.get("bathsRange") or [0.0]
        size_range = node.get("sizeRange") or []

        ptype = _map_property_type(node.get("listingType"))

        addr_obj = node.get("address") or {}
        street = addr_obj.get("street") or ""
        postal = addr_obj.get("postalCode")
        loc = node.get("rentalListingLocation") or [None, None]
        lng, lat = (loc[0], loc[1]) if len(loc) >= 2 else (None, None)

        photo = _pick_image(node.get("images") or [])
        path = node.get("path") or ""
        source_url = f"{BASE_URL}/{path}" if path else BASE_URL

        phone, phone_ext = normalize_phone((node.get("contact") or {}).get("phoneNumber"))

        return Listing(
            id=_stable_id("rentals_ca", nid),
            source="rentals.ca",
            source_url=source_url,
            title=str(node.get("rentalListingName") or "").strip()[:200] or "Rental",
            price=float(price),
            bedrooms=float(beds_range[0]) if beds_range else 0.0,
            bathrooms=float(baths_range[0]) if baths_range else 0.0,
            sqft=sane_sqft(size_range[0]) if size_range else None,
            property_type=ptype,
            address=street or None,
            postal_code=postal,
            lat=float(lat) if lat is not None else None,
            lng=float(lng) if lng is not None else None,
            phone=phone,
            phone_ext=phone_ext,
            pets_allowed=_pets_from_options(node.get("petOptions")),
            parking=_parking_available(node.get("parking")),
            photos=[photo] if photo else [],
        )
    except Exception as e:
        log.warning("rentals.ca parse error for id=%s: %s", node.get("id"), e)
        return None


class RentalsCaScraper(Scraper):
    name = "rentals.ca"

    def __init__(
        self,
        concurrency: int = 4,
        max_retries: int = 2,
        backoff: float = 0.6,
        wave_delay: float = 0.25,
    ) -> None:
        # rentals.ca rate-limits hard (HTTP 429/403). Since the pool is only
        # scraped ~once per cache-TTL, we favour gentle-and-reliable over fast:
        # few concurrent requests, a small gap between waves, and per-page retries.
        self._concurrency = concurrency
        self._max_retries = max_retries
        self._backoff = backoff
        self._wave_delay = wave_delay
        self._impersonate = "chrome124"

    async def scrape(self, filters: SearchFilters) -> list[Listing]:
        async def fetch_page(page: int) -> Optional[list[Listing]]:
            """Returns the page's listings, or None if the request failed after
            retries. An empty list means a successful fetch of a page past the
            last result."""
            def _do() -> list[Listing]:
                with cr.Session(impersonate=self._impersonate) as s:
                    r = s.get(f"{SEARCH_URL}?p={page}", timeout=25)
                    r.raise_for_status()
                data = _extract_response_json(r.text)
                if not data:
                    return []
                edges = data.get("data", {}).get("edges") or []
                return [l for l in (_parse_node(e.get("node") or {}) for e in edges) if l]

            for attempt in range(self._max_retries + 1):
                try:
                    return await asyncio.to_thread(_do)
                except Exception as e:
                    if attempt < self._max_retries:
                        # Exponential backoff with jitter to ride out a 429/403.
                        await asyncio.sleep(self._backoff * (2 ** attempt) + random.uniform(0, 0.3))
                        continue
                    log.warning("rentals.ca page %d failed after %d attempts: %s", page, attempt + 1, e)
                    return None
            return None

        # Edmonton has far fewer than MAX_PAGES pages of results. Fetching them all
        # in one burst both wastes requests and gets pages rate-limited. Instead
        # fetch in small waves and stop as soon as results run out — signalled by a
        # successfully-fetched empty page, or a wave that adds no new listings (some
        # out-of-range pages echo the last page rather than returning empty). A wave
        # where *every* page errored doesn't stop us, so a transient burst of 429s
        # can't silently truncate the results.
        listings: list[Listing] = []
        seen_ids: set[str] = set()
        pages_fetched = 0
        wave_starts = list(range(1, MAX_PAGES + 1, self._concurrency))
        for wi, wave_start in enumerate(wave_starts):
            wave_pages = list(range(wave_start, min(wave_start + self._concurrency, MAX_PAGES + 1)))
            wave = await asyncio.gather(*(fetch_page(p) for p in wave_pages))
            pages_fetched += len(wave_pages)

            reached_end = False
            new_in_wave = 0
            any_success = False
            for batch in wave:
                if batch is None:
                    continue  # error even after retries — ignore, keep paginating
                any_success = True
                if not batch:
                    reached_end = True  # valid page with no results => past the end
                    continue
                for l in batch:
                    if l.id in seen_ids:
                        continue
                    seen_ids.add(l.id)
                    listings.append(l)
                    new_in_wave += 1

            if reached_end or (any_success and new_in_wave == 0):
                break
            if wi < len(wave_starts) - 1:
                await asyncio.sleep(self._wave_delay)

        log.info(
            "rentals.ca scraped %d listings across %d pages (cap %d)",
            len(listings), pages_fetched, MAX_PAGES,
        )
        return listings
