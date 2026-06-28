"""RentCanada scraper.

rentcanada.com embeds its Edmonton search results as an inline
`window.searchResult = { "total": N, "currentPage": 1, "listings": [ ... ] }`
assignment on each search page. We read page 1 to learn the total count, then
fetch the remaining `?p=N` pages concurrently and map each entry to a Listing.

Quirks of RentCanada's data (all handled in `_parse_listing`):
  * One card is a *property* with min/max ranges, not a single unit — we take the
    minimum of each (`minRate` / `minBeds` / `minBaths` / `minSqFt`), mirroring
    `rentals_ca.py`'s use of `rentRange[0]`.
  * `active` is `False` on every search-feed listing (it is not a "live" flag) —
    so we never filter on it.
  * `description` is HTML — stripped to plain text.
  * Amenity-ish data is split across `amenities`, `utilities`, `petPolicies`, and
    `parkingPolicies`, each a list of `{name, value}`; `value` is a clean
    kebab-case slug we keyword-match for the boolean flags.
  * Pagination is `?p=N` (NOT `?page=N`, which the server ignores). Page 1 carries
    `total`, so we fetch exactly ⌈total/20⌉ pages (capped at MAX_PAGES).
  * No phone in the search feed; the number lives on the per-listing detail page
    (inside `window.pageData`) and is resolved lazily on demand
    (`fetch_listing_phone`) behind the shared "Contact" button — exactly like Zumper.
"""

from __future__ import annotations

import asyncio
import hashlib
import html as _html
import json
import logging
import re
from typing import Any, Optional

from curl_cffi import requests as cr  # type: ignore[import-not-found]

from app.models import Listing, PropertyType, SearchFilters
from app.scrapers.base import Scraper, sane_sqft

log = logging.getLogger(__name__)

BASE_URL = "https://www.rentcanada.com"
SEARCH_PATH = "/edmonton-ab"
PER_PAGE = 20
MAX_PAGES = 75  # ~1400 listings / 20 ≈ 70 pages; small cushion above today's count

_SEARCH_ANCHOR_RE = re.compile(r"window\.searchResult\s*=\s*(\{)")
_PAGEDATA_ANCHOR_RE = re.compile(r"window\.pageData\s*=\s*(\{)")
_TAG_RE = re.compile(r"<[^>]+>")

_TYPE_MAP = {
    "apartment": PropertyType.APARTMENT,
    "condo": PropertyType.CONDO,
    "condominium": PropertyType.CONDO,
    "townhouse": PropertyType.TOWNHOUSE,
    "house": PropertyType.HOUSE,
    "main floor": PropertyType.HOUSE,
    "duplex": PropertyType.HOUSE,
    "triplex": PropertyType.HOUSE,
    "basement": PropertyType.BASEMENT,
    "room": PropertyType.ROOM,
    "loft": PropertyType.APARTMENT,
}

# Boolean amenity flags, matched by substring against the joined amenity `value`
# slugs. Ordered tuples = any-match. "in-suite" laundry is matched specifically so
# building/shared laundry ("laundry-on-site", "laundry-facilities") doesn't count.
_AMENITY_KEYWORDS = {
    "in_suite_laundry": ("in-suite-laundry", "washer-in-suite", "dryer-in-suite", "laundry-in-suite", "in-suite-laundry-facilities", "laundry---in-suite"),
    "dishwasher": ("dishwasher",),
    "ac": ("air-condition", "central-air", "ac-unit"),
    "balcony": ("balcon", "patio"),
    "gym": ("gym", "fitness", "exercise-room", "free-weights"),
}
_PET_POSITIVE = {"pet-friendly", "cat-friendly", "small-dog-friendly", "large-dog-friendly", "dog-friendly"}
_PET_NEGATIVE = {"pets-not-allowed", "no-pets", "not-pet-friendly"}


def _stable_id(source: str, source_id: Any) -> str:
    return hashlib.sha256(f"{source}:{source_id}".encode()).hexdigest()[:24]


def _extract_balanced(html: str, start: int) -> Optional[str]:
    """Return the balanced `{...}` substring beginning at `start` (the opening brace),
    correctly skipping braces inside JSON strings. None if never balanced."""
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
                    return html[start : i + 1]
        i += 1
    return None


def _extract_search(html: str) -> Optional[dict[str, Any]]:
    """Parse the inline `window.searchResult = { ... }` object from a search page."""
    m = _SEARCH_ANCHOR_RE.search(html)
    if not m:
        return None
    raw = _extract_balanced(html, m.start(1))
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _to_float(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_int(v: Any) -> Optional[int]:
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _strip_html(s: Any) -> str:
    """Turn an HTML description blob into plain text (unescape entities, drop tags)."""
    if not s:
        return ""
    text = _TAG_RE.sub(" ", str(s))
    return re.sub(r"\s+", " ", _html.unescape(text)).strip()


def _values(items: Any) -> list[str]:
    """Pull the kebab-case `value` slugs out of an [{name, value}, ...] list."""
    out: list[str] = []
    for it in items or []:
        if isinstance(it, dict) and it.get("value"):
            out.append(str(it["value"]).lower())
    return out


def _names(items: Any) -> list[str]:
    out: list[str] = []
    for it in items or []:
        if isinstance(it, dict) and it.get("name"):
            out.append(str(it["name"]).strip())
    return out


def _flag(slugs_joined: str, keywords: tuple[str, ...]) -> Optional[bool]:
    """True if any keyword appears in the joined amenity slugs; None when there are
    no amenities at all (unknown, not 'absent')."""
    if not slugs_joined:
        return None
    return True if any(k in slugs_joined for k in keywords) else None


def _pets(pet_slugs: list[str], amenity_slugs: list[str]) -> Optional[bool]:
    pool = set(pet_slugs) | set(amenity_slugs)
    if pool & _PET_POSITIVE:
        return True
    if pool & _PET_NEGATIVE:
        return False
    return None


def _parse_listing(raw: dict[str, Any]) -> Optional[Listing]:
    try:
        lid = raw.get("id")
        if lid is None:
            return None

        price = _to_float(raw.get("minRate"))
        if price is None or price <= 0:
            return None  # "Call for pricing" / no published rate

        ptype = _TYPE_MAP.get(str(raw.get("propertyType") or "").strip().lower(), PropertyType.OTHER)

        amenity_slugs = _values(raw.get("amenities"))
        parking_slugs = _values(raw.get("parkingPolicies"))
        pet_slugs = _values(raw.get("petPolicies"))
        joined = " ".join(amenity_slugs)

        in_suite = _flag(joined, _AMENITY_KEYWORDS["in_suite_laundry"])
        dishwasher = _flag(joined, _AMENITY_KEYWORDS["dishwasher"])
        ac = _flag(joined, _AMENITY_KEYWORDS["ac"])
        balcony = _flag(joined, _AMENITY_KEYWORDS["balcony"])
        gym = _flag(joined, _AMENITY_KEYWORDS["gym"])
        furnished: Optional[bool] = None
        if "furnished" in amenity_slugs:
            furnished = True
        elif "not-furnished" in amenity_slugs or "unfurnished" in amenity_slugs:
            furnished = False

        # Parking: an explicit parking policy is the clean signal; fall back to an
        # amenity slug mentioning parking/garage/parkade. None = unknown.
        if parking_slugs:
            parking: Optional[bool] = True
        elif any(("parking" in s or "garage" in s or "parkade" in s or "carport" in s) for s in amenity_slugs):
            parking = True
        else:
            parking = None

        url_path = raw.get("url") or raw.get("previewUrl") or ""
        source_url = (BASE_URL + url_path) if url_path.startswith("/") else (url_path or BASE_URL)

        photo = raw.get("photo")
        photos = [str(photo)] if photo else []

        amenities: list[str] = [f"{u} included" for u in _names(raw.get("utilities"))]
        for present, label in (
            (in_suite, "In-suite laundry"), (parking, "Parking"), (dishwasher, "Dishwasher"),
            (ac, "A/C"), (balcony, "Balcony"), (gym, "Gym"),
        ):
            if present:
                amenities.append(label)

        return Listing(
            id=_stable_id("rentcanada", lid),
            source="rentcanada",
            source_url=source_url,
            title=str(raw.get("name") or raw.get("address") or "Rental").strip()[:200] or "Rental",
            price=price,
            bedrooms=_to_float(raw.get("minBeds")) or 0.0,
            bathrooms=_to_float(raw.get("minBaths")) or 0.0,
            sqft=sane_sqft(raw.get("minSqFt")),
            property_type=ptype,
            address=str(raw.get("address") or raw.get("streetName") or "").strip() or None,
            neighborhood=str(raw.get("neighbourhood") or "").strip() or None,
            city=str(raw.get("city") or "Edmonton").strip() or "Edmonton",
            postal_code=str(raw.get("postalCode") or "").strip() or None,
            lat=_to_float(raw.get("latitude")),
            lng=_to_float(raw.get("longitude")),
            pets_allowed=_pets(pet_slugs, amenity_slugs),
            parking=parking,
            in_suite_laundry=in_suite,
            furnished=furnished,
            dishwasher=dishwasher,
            ac=ac,
            balcony=balcony,
            gym=gym,
            year_built=_to_int(raw.get("yearBuilt")),
            photos=photos,
            description=_strip_html(raw.get("description")),
            amenities=amenities,
        )
    except Exception as e:
        log.warning("rentcanada parse error for id=%s: %s", raw.get("id"), e)
        return None


def _extract_detail_phone(data: Any) -> Optional[str]:
    """Find the first usable contact number inside a parsed detail-page `pageData`
    object. Prefers an explicit `pmPhone`, else the first `contacts[].phone`. The
    listing is nested somewhere inside pageData, so we walk it; pmPhone short-circuits
    (most-specific), otherwise the first contact phone seen is used."""
    pm: list[Optional[str]] = [None]
    contact: list[Optional[str]] = [None]

    def walk(o: Any) -> None:
        if pm[0] is not None:
            return
        if isinstance(o, dict):
            v = o.get("pmPhone")
            if isinstance(v, str) and any(c.isdigit() for c in v):
                pm[0] = v
                return
            if contact[0] is None:
                for c in o.get("contacts") or []:
                    if isinstance(c, dict) and c.get("phone"):
                        contact[0] = str(c["phone"])
                        break
            for val in o.values():
                walk(val)
        elif isinstance(o, list):
            for val in o:
                walk(val)

    walk(data)
    return pm[0] or contact[0]


async def fetch_listing_phone(
    source_url: str, impersonate: str = "chrome124", attempts: int = 3
) -> tuple[bool, Optional[str]]:
    """Fetch a RentCanada detail page and resolve its contact number. Returns
    `(fetched_ok, raw_phone)` with the same result-aware contract as the Zumper
    fetcher, so the caller can cache definitive outcomes forever and retry transient
    ones:

    - `(True, "...")`  — page parsed, a number was found.
    - `(True, None)`   — page parsed cleanly, but the listing has no number.
    - `(False, None)`  — fetch/parse failed (HTTP error, timeout, or a block with no
                         `window.pageData`); transient, so do NOT cache. Retried first.
    """
    def _do() -> Optional[str]:
        with cr.Session(impersonate=impersonate) as s:
            r = s.get(source_url, timeout=25)
            r.raise_for_status()
        m = _PAGEDATA_ANCHOR_RE.search(r.text)
        if not m:
            # No pageData usually means a challenge/blocked page, not a real
            # "no number" — raise so it's treated as transient (retryable).
            raise ValueError("no window.pageData on detail page (likely blocked)")
        raw = _extract_balanced(r.text, m.start(1))
        if not raw:
            raise ValueError("window.pageData not balanced")
        return _extract_detail_phone(json.loads(raw))

    for attempt in range(attempts):
        try:
            return True, await asyncio.to_thread(_do)
        except Exception as e:
            if attempt + 1 < attempts:
                await asyncio.sleep(0.6 * (attempt + 1))
                continue
            log.warning(
                "rentcanada detail phone fetch failed for %s after %d attempts: %s",
                source_url, attempts, e,
            )
            return False, None
    return False, None


class RentCanadaScraper(Scraper):
    name = "rentcanada"

    def __init__(self, concurrency: int = 6) -> None:
        self._concurrency = concurrency
        self._impersonate = "chrome124"

    def _page_url(self, page: int) -> str:
        return f"{BASE_URL}{SEARCH_PATH}" if page <= 1 else f"{BASE_URL}{SEARCH_PATH}?p={page}"

    async def scrape(self, filters: SearchFilters) -> list[Listing]:
        def _fetch(page: int) -> Optional[dict[str, Any]]:
            with cr.Session(impersonate=self._impersonate) as s:
                r = s.get(self._page_url(page), timeout=30)
                r.raise_for_status()
            return _extract_search(r.text)

        # Page 1 first — it carries the total count that drives pagination.
        try:
            data1 = await asyncio.to_thread(_fetch, 1)
        except Exception as e:
            log.exception("rentcanada page 1 fetch failed: %s", e)
            return []
        if not data1:
            log.warning("rentcanada page 1 had no searchResult JSON")
            return []

        total = data1.get("total")
        pages = MAX_PAGES
        if isinstance(total, int) and total > 0:
            pages = min(MAX_PAGES, max(1, -(-total // PER_PAGE)))  # ceil div

        sem = asyncio.Semaphore(self._concurrency)

        async def fetch_rest(page: int) -> list[dict[str, Any]]:
            async with sem:
                try:
                    data = await asyncio.to_thread(_fetch, page)
                    return (data or {}).get("listings") or []
                except Exception as e:
                    log.warning("rentcanada page %d failed: %s", page, e)
                    return []

        rest = await asyncio.gather(*(fetch_rest(p) for p in range(2, pages + 1)))

        listings: list[Listing] = []
        seen: set[str] = set()
        for batch in [data1.get("listings") or [], *rest]:
            for raw in batch:
                l = _parse_listing(raw)
                if l is None or l.id in seen:
                    continue
                seen.add(l.id)
                listings.append(l)

        log.info(
            "rentcanada scraped %d listings across %d pages (total reported %s)",
            len(listings), pages, total,
        )
        return listings
