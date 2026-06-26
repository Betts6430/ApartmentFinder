"""Zumper scraper.

Zumper embeds search results in `window.__PRELOADED_STATE__` on each search page,
under `currentSearch.listables.listables`. We paginate via `?page=N` and extract
the inline JSON. ~25 results per page, ~110 pages for Edmonton.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from typing import Any, Optional

from curl_cffi import requests as cr  # type: ignore[import-not-found]

from app.models import Listing, PropertyType, SearchFilters
from app.scrapers.base import Scraper, normalize_phone, sane_sqft

log = logging.getLogger(__name__)

BASE_URL = "https://www.zumper.com"
SEARCH_PATH = "/apartments-for-rent/edmonton-ab"
IMAGE_BASE = "https://img.zumpercdn.com"
MAX_PAGES = 30  # caps Zumper scrape ~7s; lots of overlap with other sources past this

_ANCHOR_RE = re.compile(r"window\.__PRELOADED_STATE__\s*=\s*(\{)")

# Zumper amenity_tags are plain strings — we just check for substring presence
_AMENITY_KEYWORDS = {
    "in_suite_laundry": ("in-unit laundry", "in-suite laundry", "washer in-suite", "washer/dryer in unit"),
    "dishwasher": ("dishwasher",),
    "ac": ("air conditioning", "central air"),
    "balcony": ("balcony", "patio"),
    "gym": ("fitness center", "gym", "exercise room"),
    "parking": ("garage parking", "assigned parking", "parking included", "covered parking", "off-street parking"),
}


def _stable_id(source: str, source_id: Any) -> str:
    return hashlib.sha256(f"{source}:{source_id}".encode()).hexdigest()[:24]


def _extract_state(html: str) -> Optional[dict[str, Any]]:
    m = _ANCHOR_RE.search(html)
    if not m:
        return None
    start = m.end() - 1
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


def _amenity_present(tags: list[str], keywords: tuple[str, ...]) -> Optional[bool]:
    """True if any keyword appears in the (lowercased) tag list. None if tag list is empty."""
    if not tags:
        return None
    joined = " | ".join(t.lower() for t in tags)
    return any(k in joined for k in keywords) or None


def _parse_node(node: dict[str, Any]) -> Optional[Listing]:
    try:
        lid = node.get("listing_id") or node.get("pb_id")
        if not lid:
            return None

        price = node.get("min_price") or node.get("max_price")
        if not price or price <= 0:
            return None

        url_path = node.get("url") or ""
        if url_path.startswith("/"):
            source_url = BASE_URL + url_path
        elif url_path.startswith("http"):
            source_url = url_path
        else:
            source_url = BASE_URL

        title = node.get("building_name") or node.get("title") or "Rental"

        img_ids = node.get("image_ids") or []
        photos = [f"{IMAGE_BASE}/{i}/640x480" for i in img_ids[:5]]

        all_tags = (node.get("amenity_tags") or []) + (node.get("building_amenity_tags") or [])

        # pets: list of category ints — non-empty means pets are allowed in some form.
        # A missing key is "unknown" (None), not "not allowed" (False).
        raw_pets = node.get("pets")
        pets_allowed: Optional[bool] = None if raw_pets is None else bool(raw_pets)

        return Listing(
            id=_stable_id("zumper", lid),
            source="zumper",
            source_url=source_url,
            title=str(title).strip()[:200],
            price=float(price),
            bedrooms=float(node.get("min_bedrooms") or 0),
            bathrooms=float(node.get("min_bathrooms") or 0),
            sqft=sane_sqft(node.get("min_square_feet")),
            property_type=PropertyType.APARTMENT,  # Zumper search is apartments-for-rent
            address=str(node.get("address") or "").strip() or None,
            neighborhood=str(node.get("neighborhood_name") or "").strip() or None,
            city=str(node.get("city") or "Edmonton"),
            postal_code=str(node.get("zipcode") or "").strip() or None,
            lat=float(node["lat"]) if node.get("lat") is not None else None,
            lng=float(node["lng"]) if node.get("lng") is not None else None,
            phone=normalize_phone(node.get("phone")),
            pets_allowed=pets_allowed,
            parking=_amenity_present(all_tags, _AMENITY_KEYWORDS["parking"]),
            in_suite_laundry=_amenity_present(all_tags, _AMENITY_KEYWORDS["in_suite_laundry"]),
            dishwasher=_amenity_present(all_tags, _AMENITY_KEYWORDS["dishwasher"]),
            ac=_amenity_present(all_tags, _AMENITY_KEYWORDS["ac"]),
            balcony=_amenity_present(all_tags, _AMENITY_KEYWORDS["balcony"]),
            gym=_amenity_present(all_tags, _AMENITY_KEYWORDS["gym"]),
            photos=photos,
            description=str(node.get("short_description") or "").strip(),
            amenities=sorted({str(t).strip() for t in all_tags if t}),
        )
    except Exception as e:
        log.warning("zumper parse error for id=%s: %s", node.get("listing_id"), e)
        return None


class ZumperScraper(Scraper):
    name = "zumper"

    def __init__(self, concurrency: int = 8) -> None:
        self._concurrency = concurrency
        self._impersonate = "chrome124"

    async def scrape(self, filters: SearchFilters) -> list[Listing]:
        sem = asyncio.Semaphore(self._concurrency)

        async def fetch_page(page: int) -> list[Listing]:
            async with sem:
                def _do() -> list[Listing]:
                    with cr.Session(impersonate=self._impersonate) as s:
                        url = f"{BASE_URL}{SEARCH_PATH}?page={page}"
                        r = s.get(url, timeout=25)
                        r.raise_for_status()
                    state = _extract_state(r.text)
                    if not state:
                        return []
                    nodes = (
                        state.get("currentSearch", {})
                        .get("listables", {})
                        .get("listables", [])
                        or []
                    )
                    return [l for l in (_parse_node(n) for n in nodes) if l]

                try:
                    return await asyncio.to_thread(_do)
                except Exception as e:
                    log.warning("zumper page %d failed: %s", page, e)
                    return []

        results = await asyncio.gather(*(fetch_page(p) for p in range(1, MAX_PAGES + 1)))

        listings: list[Listing] = []
        seen: set[str] = set()
        for batch in results:
            for l in batch:
                if l.id in seen:
                    continue
                seen.add(l.id)
                listings.append(l)

        log.info("zumper scraped %d listings across up to %d pages", len(listings), MAX_PAGES)
        return listings
