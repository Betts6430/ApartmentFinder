"""Kijiji scraper.

Kijiji (Next.js) embeds its search results in a `<script id="__NEXT_DATA__">` JSON
blob, inside a normalized Apollo cache at `props.pageProps.__APOLLO_STATE__`. Each
listing is a `RealEstateListing` entry; per-page totals live under
`pagination.totalCount`. We read page 1 to learn the total, then fetch the
remaining pages concurrently.

Quirks of Kijiji's data:
  * `price.amount` is in **cents** (154800 -> $1548.00); may be null (no price).
  * `numberbathrooms` is encoded **x10** ("20" -> 2.0 baths, "15" -> 1.5).
  * `numberbedrooms` is plain ("0" -> studio).
  * structured fields live in `attributes.all` as canonicalName/canonicalValues.
  * the feed carries no phone number — Kijiji gates it behind detail-page
    interaction — so Kijiji listings fall through to the "Contact on source" link.
  * "TOP_AD" promoted listings repeat across pages; we dedupe by id.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from datetime import date, datetime
from typing import Any, Optional

from curl_cffi import requests as cr  # type: ignore[import-not-found]

from app.models import Listing, PropertyType, SearchFilters
from app.scrapers.base import Scraper, sane_sqft

log = logging.getLogger(__name__)

BASE_URL = "https://www.kijiji.ca"
# Edmonton "Apartments & Condos" (category 37, location 1700203). Despite the
# slug, the feed mixes in house / townhouse / duplex unit types too.
CATEGORY_PATH = "b-apartments-condos/edmonton"
CATEGORY_SUFFIX = "c37l1700203"
PER_PAGE = 40  # organic listings per page (plus a few repeated top-ads)
MAX_PAGES = 45  # ~1800 listings cap; Edmonton runs ~1700

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S
)
_POSTAL_RE = re.compile(r"[A-Za-z]\d[A-Za-z]\s*\d[A-Za-z]\d")

_TYPE_MAP = {
    "apartment": PropertyType.APARTMENT,
    "condo": PropertyType.CONDO,
    "condominium": PropertyType.CONDO,
    "townhouse": PropertyType.TOWNHOUSE,
    "house": PropertyType.HOUSE,
    "duplex-triplex": PropertyType.HOUSE,
    "duplex": PropertyType.HOUSE,
    "basement": PropertyType.BASEMENT,
    "room": PropertyType.ROOM,
    "loft": PropertyType.APARTMENT,
}

# Boolean amenity attributes -> human-readable label (used for amenities list).
_BOOL_AMENITIES = {
    "laundryinunit": "In-suite laundry",
    "laundryinbuilding": "Laundry in building",
    "dishwasher": "Dishwasher",
    "airconditioning": "A/C",
    "balcony": "Balcony",
    "gym": "Gym",
    "pool": "Pool",
    "elevator": "Elevator",
    "concierge": "Concierge",
    "storagelocker": "Storage locker",
    "furnished": "Furnished",
    "twentyfourhoursecurity": "24h security",
}
# Utilities: "1" means included.
_UTILITIES = {
    "heat": "Heat included",
    "water": "Water included",
    "hydro": "Hydro included",
    "internet": "Internet included",
    "cabletv": "Cable TV included",
}


def _stable_id(source: str, source_id: Any) -> str:
    return hashlib.sha256(f"{source}:{source_id}".encode()).hexdigest()[:24]


def _extract_listings(html: str) -> tuple[list[dict[str, Any]], Optional[int]]:
    """Return (RealEstateListing dicts, totalCount) from a Kijiji search page."""
    m = _NEXT_DATA_RE.search(html)
    if not m:
        return [], None
    try:
        apollo = json.loads(m.group(1))["props"]["pageProps"]["__APOLLO_STATE__"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return [], None

    listings = [
        v for v in apollo.values()
        if isinstance(v, dict) and v.get("__typename") == "RealEstateListing"
    ]

    total: Optional[int] = None
    for k, v in apollo.get("ROOT_QUERY", {}).items():
        if k.startswith("searchResultsPageByUrl") and isinstance(v, dict):
            pg = v.get("pagination") or {}
            if isinstance(pg.get("totalCount"), int):
                total = pg["totalCount"]
                break
    return listings, total


def _attrs(raw: dict[str, Any]) -> dict[str, list[str]]:
    """Flatten the attributes.all list into {canonicalName: canonicalValues}."""
    out: dict[str, list[str]] = {}
    for a in (raw.get("attributes") or {}).get("all") or []:
        name = a.get("canonicalName")
        if name:
            out[name] = a.get("canonicalValues") or []
    return out


def _first(values: Optional[list[str]]) -> Optional[str]:
    return values[0] if values else None


def _to_float(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _bool_attr(values: Optional[list[str]]) -> Optional[bool]:
    """Kijiji yes/no attributes are "1"/"0". None when missing."""
    v = _first(values)
    if v is None:
        return None
    return v == "1"


def _pets(values: Optional[list[str]]) -> Optional[bool]:
    v = _first(values)
    if v is None:
        return None
    if v in ("1", "limited"):  # "limited" = some pets allowed
        return True
    if v == "0":
        return False
    return None


def _available_date(values: Optional[list[str]]) -> Optional[date]:
    v = _first(values)
    if not v:
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _bigger_image(url: str) -> str:
    """Swap Kijiji's 200px thumbnail rule for a card-sized one."""
    return re.sub(r"rule=kijijica-\d+-jpg", "rule=kijijica-960-jpg", url)


_PROVINCES = {"AB", "BC", "SK", "MB", "ON", "QC", "NB", "NS", "PE", "NL", "YT", "NT", "NU"}


def _split_address(full: Optional[str]) -> tuple[Optional[str], str, Optional[str]]:
    """Kijiji addresses look like "10616 84 Avenue NW, Edmonton, AB, T4X 2A7", but
    private listings often hide the street ("Edmonton, AB, T6L 1B7"). Return
    (street, city, postal_code), treating a part as a street only if it has a digit."""
    if not full:
        return None, "Edmonton", None
    postal_m = _POSTAL_RE.search(full)
    postal = re.sub(r"\s+", " ", postal_m.group(0).upper()).strip() if postal_m else None
    parts = [p.strip() for p in _POSTAL_RE.sub("", full).split(",") if p.strip()]
    parts = [p for p in parts if p.upper() not in _PROVINCES]

    street: Optional[str] = None
    city = "Edmonton"
    if parts:
        if any(ch.isdigit() for ch in parts[0]):  # a real street has a number
            street = parts[0]
            if len(parts) > 1:
                city = parts[1]
        else:
            city = parts[0]
    return street, city or "Edmonton", postal


def _parse_listing(raw: dict[str, Any]) -> Optional[Listing]:
    try:
        lid = raw.get("id")
        if not lid:
            return None

        amount = (raw.get("price") or {}).get("amount")
        if not isinstance(amount, (int, float)) or amount <= 0:
            return None  # null price or "Please Contact" listing
        price = float(amount) / 100.0  # cents -> dollars

        attrs = _attrs(raw)

        beds = _to_float(_first(attrs.get("numberbedrooms"))) or 0.0
        baths_raw = _to_float(_first(attrs.get("numberbathrooms")))
        baths = (baths_raw / 10.0) if baths_raw is not None else 0.0

        ptype = _TYPE_MAP.get(
            (_first(attrs.get("unittype")) or "").lower(), PropertyType.OTHER
        )

        loc = raw.get("location") or {}
        street, city, postal = _split_address(loc.get("address"))
        coords = loc.get("coordinates") or {}
        name = str(loc.get("name") or "").strip()
        neighborhood = name if name and name.lower() != city.lower() else None

        photos = [_bigger_image(u) for u in (raw.get("imageUrls") or [])[:5] if u]

        parking_spots = _to_float(_first(attrs.get("numberparkingspots")))
        parking = None if parking_spots is None else parking_spots > 0

        amenities: list[str] = []
        for key, label in _BOOL_AMENITIES.items():
            if _bool_attr(attrs.get(key)):
                amenities.append(label)
        for key, label in _UTILITIES.items():
            if _first(attrs.get(key)) == "1":
                amenities.append(label)
        if parking:
            amenities.append("Parking")

        in_suite = _bool_attr(attrs.get("laundryinunit"))

        return Listing(
            id=_stable_id("kijiji", lid),
            source="kijiji",
            source_url=str(raw.get("url") or BASE_URL),
            title=str(raw.get("title") or "").strip()[:200] or "Rental",
            price=price,
            bedrooms=beds,
            bathrooms=baths,
            sqft=sane_sqft(_first(attrs.get("areainfeet"))),
            property_type=ptype,
            address=street,
            neighborhood=neighborhood,
            city=city,
            postal_code=postal,
            lat=_to_float(coords.get("latitude")),
            lng=_to_float(coords.get("longitude")),
            pets_allowed=_pets(attrs.get("petsallowed")),
            parking=parking,
            in_suite_laundry=in_suite,
            furnished=_bool_attr(attrs.get("furnished")),
            dishwasher=_bool_attr(attrs.get("dishwasher")),
            ac=_bool_attr(attrs.get("airconditioning")),
            balcony=_bool_attr(attrs.get("balcony")),
            gym=_bool_attr(attrs.get("gym")),
            available_date=_available_date(attrs.get("dateavailable")),
            photos=photos,
            description=str(raw.get("description") or "").strip(),
            amenities=amenities,
        )
    except Exception as e:
        log.warning("kijiji parse error for id=%s: %s", raw.get("id"), e)
        return None


class KijijiScraper(Scraper):
    name = "kijiji"

    def __init__(self, concurrency: int = 6) -> None:
        self._concurrency = concurrency
        self._impersonate = "chrome124"

    def _page_url(self, page: int) -> str:
        if page <= 1:
            return f"{BASE_URL}/{CATEGORY_PATH}/{CATEGORY_SUFFIX}"
        return f"{BASE_URL}/{CATEGORY_PATH}/page-{page}/{CATEGORY_SUFFIX}"

    async def scrape(self, filters: SearchFilters) -> list[Listing]:
        def _fetch(page: int) -> tuple[list[dict[str, Any]], Optional[int]]:
            with cr.Session(impersonate=self._impersonate) as s:
                r = s.get(self._page_url(page), timeout=30)
                r.raise_for_status()
            return _extract_listings(r.text)

        # Page 1 first — it tells us the total result count.
        try:
            raw_page1, total = await asyncio.to_thread(_fetch, 1)
        except Exception as e:
            log.exception("kijiji page 1 fetch failed: %s", e)
            return []

        pages = MAX_PAGES
        if total:
            pages = min(MAX_PAGES, max(1, -(-total // PER_PAGE)))  # ceil div

        sem = asyncio.Semaphore(self._concurrency)

        async def fetch_rest(page: int) -> list[dict[str, Any]]:
            async with sem:
                try:
                    raw, _ = await asyncio.to_thread(_fetch, page)
                    return raw
                except Exception as e:
                    log.warning("kijiji page %d failed: %s", page, e)
                    return []

        rest = await asyncio.gather(*(fetch_rest(p) for p in range(2, pages + 1)))

        listings: list[Listing] = []
        seen: set[str] = set()
        for batch in [raw_page1, *rest]:
            for raw in batch:
                l = _parse_listing(raw)
                if l is None or l.id in seen:
                    continue
                seen.add(l.id)
                listings.append(l)

        log.info(
            "kijiji scraped %d listings across %d pages (total reported %s)",
            len(listings), pages, total,
        )
        return listings
