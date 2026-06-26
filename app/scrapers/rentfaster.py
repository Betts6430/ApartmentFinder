"""RentFaster scraper.

Uses RentFaster's public map.json endpoint, accessed with a Chrome TLS fingerprint
to satisfy Cloudflare. The endpoint returns ~500 active Edmonton listings per call
with rich, structured fields (no HTML parsing needed).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any, Optional

from curl_cffi import requests as cr  # type: ignore[import-not-found]

from app.models import Listing, PropertyType, SearchFilters
from app.scrapers.base import Scraper, normalize_phone, sane_sqft

log = logging.getLogger(__name__)

EDMONTON_CITY_ID = 2
API_URL = f"https://www.rentfaster.ca/api/map.json?city_id={EDMONTON_CITY_ID}"
BASE_URL = "https://www.rentfaster.ca"

_TYPE_MAP = {
    "apartment": PropertyType.APARTMENT,
    "condo": PropertyType.CONDO,
    "condo unit": PropertyType.CONDO,
    "townhouse": PropertyType.TOWNHOUSE,
    "house": PropertyType.HOUSE,
    "main floor": PropertyType.HOUSE,
    "basement": PropertyType.BASEMENT,
    "room": PropertyType.ROOM,
    "loft": PropertyType.APARTMENT,
    "duplex": PropertyType.HOUSE,
    "fourplex": PropertyType.APARTMENT,
    "mobile": PropertyType.OTHER,
}


def _stable_id(source: str, source_id: Any) -> str:
    return hashlib.sha256(f"{source}:{source_id}".encode()).hexdigest()[:24]


def _to_int(v: Any) -> Optional[int]:
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _to_float(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_bool_pos(v: Any) -> Optional[bool]:
    """RentFaster uses 0/1 ints for amenities. None if missing."""
    if v in (None, ""):
        return None
    try:
        return int(v) > 0
    except (TypeError, ValueError):
        return None


def _parse_listing(raw: dict[str, Any]) -> Optional[Listing]:
    try:
        ref_id = raw.get("ref_id") or raw.get("id")
        if ref_id is None:
            return None

        price = _to_float(raw.get("price"))
        if price is None or price <= 0:
            return None  # skip listings without a real price

        beds_str = str(raw.get("beds") or "").strip().lower()
        if beds_str in ("bachelor", "studio", ""):
            beds: float = 0.0
        else:
            beds = _to_float(beds_str) or 0.0

        baths = _to_float(raw.get("baths")) or 0.0

        ptype_raw = str(raw.get("type") or "").strip().lower()
        ptype = _TYPE_MAP.get(ptype_raw, PropertyType.OTHER)

        link = raw.get("link") or ""
        if link.startswith("/"):
            source_url = BASE_URL + link
        elif link.startswith("http"):
            source_url = link
        else:
            source_url = f"{BASE_URL}/properties/{ref_id}"

        photos: list[str] = []
        thumb = raw.get("thumb2") or raw.get("thumb")
        if thumb:
            photos.append(thumb)

        # Contact phone — digits only; accept only plausible NANP lengths.
        phone = normalize_phone(raw.get("phone"))

        cats = _to_int(raw.get("cats"))
        dogs = _to_int(raw.get("dogs"))
        pets_allowed: Optional[bool]
        if cats is None and dogs is None:
            pets_allowed = None
        else:
            pets_allowed = (cats or 0) > 0 or (dogs or 0) > 0

        amenities: list[str] = []
        for fld, label in (
            ("dishwasher", "Dishwasher"),
            ("laundry_in_suite", "In-suite laundry"),
            ("air_conditioning", "A/C"),
            ("parking_available", "Parking"),
        ):
            if _to_bool_pos(raw.get(fld)):
                amenities.append(label)
        for u in raw.get("utilities_included") or []:
            amenities.append(f"{u} included")

        return Listing(
            id=_stable_id("rentfaster", ref_id),
            source="rentfaster",
            source_url=source_url,
            title=str(raw.get("title") or raw.get("intro") or "").strip()[:200] or "Rental",
            price=price,
            bedrooms=beds,
            bathrooms=baths,
            sqft=sane_sqft(raw.get("sq_feet")),
            property_type=ptype,
            address=str(raw.get("address") or "").strip() or None,
            neighborhood=str(raw.get("community") or "").strip() or None,
            city=str(raw.get("city") or "Edmonton"),
            lat=_to_float(raw.get("latitude")),
            lng=_to_float(raw.get("longitude")),
            phone=phone,
            pets_allowed=pets_allowed,
            parking=_to_bool_pos(raw.get("parking_available")),
            in_suite_laundry=_to_bool_pos(raw.get("laundry_in_suite")),
            dishwasher=_to_bool_pos(raw.get("dishwasher")),
            ac=_to_bool_pos(raw.get("air_conditioning")),
            photos=photos,
            description=str(raw.get("intro") or "").strip(),
            amenities=amenities,
        )
    except Exception as e:
        log.warning("rentfaster parse error for ref_id=%s: %s", raw.get("ref_id"), e)
        return None


class RentFasterScraper(Scraper):
    name = "rentfaster"

    def __init__(self) -> None:
        # curl_cffi sessions aren't thread-safe but a fresh one per call is fine.
        self._impersonate = "chrome124"

    async def scrape(self, filters: SearchFilters) -> list[Listing]:
        def _fetch() -> list[dict[str, Any]]:
            with cr.Session(impersonate=self._impersonate) as s:
                r = s.get(API_URL, timeout=25)
                r.raise_for_status()
                data = r.json()
            if isinstance(data, dict):
                return data.get("listings") or []
            return []

        try:
            raw_listings = await asyncio.to_thread(_fetch)
        except Exception as e:
            log.exception("rentfaster fetch failed: %s", e)
            return []

        out: list[Listing] = []
        for raw in raw_listings:
            l = _parse_listing(raw)
            if l is not None:
                out.append(l)
        log.info("rentfaster scraped %d listings (raw=%d)", len(out), len(raw_listings))
        return out
