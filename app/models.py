from __future__ import annotations

import re
from datetime import date, datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator


# Sources spell street addresses every which way ("St" / "Street" / "ST",
# "NW" / "Northwest", ALL CAPS, ordinal "37th"). Normalize to one house style:
# expanded street-type words, uppercase quadrant abbreviations, ordinals stripped
# from numbered streets, everything else Title Cased.
_STREET_TYPES = {
    "st": "Street", "street": "Street",
    "ave": "Avenue", "av": "Avenue", "avenue": "Avenue",
    "blvd": "Boulevard", "boulevard": "Boulevard",
    "dr": "Drive", "drive": "Drive",
    "rd": "Road", "road": "Road",
    "cres": "Crescent", "crescent": "Crescent",
    "pl": "Place", "place": "Place",
    "ct": "Court", "court": "Court",
    "cir": "Circle", "circle": "Circle",
    "ter": "Terrace", "terrace": "Terrace",
    "ln": "Lane", "lane": "Lane",
    "pt": "Point", "point": "Point",
    "sq": "Square", "square": "Square",
    "gdns": "Gardens", "gardens": "Gardens",
    "wynd": "Wynd", "way": "Way", "gate": "Gate", "green": "Green",
    "hill": "Hill", "close": "Close", "bay": "Bay", "link": "Link",
    "manor": "Manor", "row": "Row", "view": "View", "common": "Common",
    "cove": "Cove", "crossing": "Crossing", "landing": "Landing",
    "loop": "Loop", "ridge": "Ridge", "run": "Run", "vista": "Vista",
    "trail": "Trail", "boulevard": "Boulevard",
}
_QUADRANTS = {
    "nw": "NW", "northwest": "NW",
    "ne": "NE", "northeast": "NE",
    "sw": "SW", "southwest": "SW",
    "se": "SE", "southeast": "SE",
}
_ORDINAL_RE = re.compile(r"^(\d+)(?:st|nd|rd|th)$", re.IGNORECASE)
_NUM_RE = re.compile(r"^\d+$")
_ALNUM_RE = re.compile(r"^\d+[a-z]$", re.IGNORECASE)  # e.g. "101A"


def normalize_address(addr: Optional[str]) -> Optional[str]:
    """Standardize a street address to one consistent house style. Returns None
    for empty/blank input."""
    if not addr or not addr.strip():
        return None
    out: list[str] = []
    for tok in addr.split():
        low = tok.lower().strip(".,")
        m = _ORDINAL_RE.match(low)
        if m:                       # "37th" -> "37"
            out.append(m.group(1))
        elif _NUM_RE.match(low):    # "10309" -> "10309"
            out.append(low)
        elif _ALNUM_RE.match(low):  # "101a" -> "101A"
            out.append(low.upper())
        elif low in _QUADRANTS:     # "Northwest" -> "NW"
            out.append(_QUADRANTS[low])
        elif low in _STREET_TYPES:  # "st" -> "Street"
            out.append(_STREET_TYPES[low])
        else:                       # "PODERSKY" -> "Podersky"
            out.append(tok[:1].upper() + tok[1:].lower())
    return " ".join(out) or None


class PropertyType(str, Enum):
    APARTMENT = "apartment"
    CONDO = "condo"
    TOWNHOUSE = "townhouse"
    HOUSE = "house"
    BASEMENT = "basement"
    ROOM = "room"
    OTHER = "other"


class SortBy(str, Enum):
    BEST_VALUE = "best_value"
    BEST_LOCATION = "best_location"
    NICEST_PLACES = "nicest_places"
    PRICE_ASC = "price_asc"
    PRICE_DESC = "price_desc"
    PRICE_DROP = "price_drop"
    NEWEST = "newest"


class SearchFilters(BaseModel):
    """User-submitted filters. All fields optional except the ones needed to scope a query."""

    price_min: Optional[int] = None
    price_max: Optional[int] = None
    bedrooms_min: Optional[int] = None
    bathrooms_min: Optional[float] = None
    sqft_min: Optional[int] = None
    property_types: list[PropertyType] = Field(default_factory=list)

    pets_allowed: Optional[bool] = None
    parking: Optional[bool] = None
    in_suite_laundry: Optional[bool] = None
    furnished: Optional[bool] = None
    dishwasher: Optional[bool] = None
    ac: Optional[bool] = None
    balcony: Optional[bool] = None
    gym: Optional[bool] = None

    move_in_by: Optional[date] = None

    # Transit-time filter
    transit_target: Optional[str] = None  # e.g. "University of Alberta"
    transit_minutes_max: Optional[int] = None
    transit_mode: str = "transit"  # transit | driving | walking | bicycling

    sort_by: SortBy = SortBy.BEST_VALUE

    @staticmethod
    def scrape_cache_key() -> str:
        """All scrapers pull the full Edmonton listing pool — there's one cache entry,
        not one per filter combination. Filters are applied to the cached pool in-memory.
        """
        return "edmonton:pool:v6"


class Listing(BaseModel):
    """Normalized rental listing — all scrapers map their output to this."""

    id: str  # stable hash of source + source_id
    source: str
    source_url: str

    title: str
    price: float
    bedrooms: float  # 0 for studio, can be 0.5 for "bachelor"
    bathrooms: float
    sqft: Optional[int] = None
    property_type: PropertyType = PropertyType.APARTMENT

    address: Optional[str] = None
    neighborhood: Optional[str] = None
    city: str = "Edmonton"
    postal_code: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None

    # Contact number, when the source exposes one (digits only; `phone_ext` holds
    # any extension for call-center / property-manager numbers). RentFaster and
    # Rentals.ca expose direct numbers in their search feed; for Zumper the number
    # lives on the detail page and is fetched lazily on demand (see main.py).
    phone: Optional[str] = None
    phone_ext: Optional[str] = None

    pets_allowed: Optional[bool] = None
    parking: Optional[bool] = None
    in_suite_laundry: Optional[bool] = None
    furnished: Optional[bool] = None
    dishwasher: Optional[bool] = None
    ac: Optional[bool] = None
    balcony: Optional[bool] = None
    gym: Optional[bool] = None

    available_date: Optional[date] = None
    year_built: Optional[int] = None

    photos: list[str] = Field(default_factory=list)
    description: str = ""
    amenities: list[str] = Field(default_factory=list)

    scraped_at: datetime = Field(default_factory=datetime.utcnow)

    # Enriched fields, set after scrape
    transit_minutes: Optional[float] = None
    prev_price: Optional[float] = None  # most recent earlier price, set when a drop is detected
    value_score: Optional[float] = None
    location_score: Optional[float] = None
    niceness_score: Optional[float] = None

    @field_validator("address")
    @classmethod
    def _normalize_address(cls, v: Optional[str]) -> Optional[str]:
        return normalize_address(v)

    def matches(self, f: SearchFilters, include_transit: bool = True) -> bool:
        """Apply user filters in-memory to a normalized listing.

        When include_transit is False the transit_minutes_max constraint is skipped —
        useful before the transit-time enrichment step has run.
        """
        if f.price_min is not None and self.price < f.price_min:
            return False
        if f.price_max is not None and self.price > f.price_max:
            return False
        if f.bedrooms_min is not None and self.bedrooms < f.bedrooms_min:
            return False
        if f.bathrooms_min is not None and self.bathrooms < f.bathrooms_min:
            return False
        if f.sqft_min is not None and (self.sqft is None or self.sqft < f.sqft_min):
            return False
        if f.property_types and self.property_type not in f.property_types:
            return False
        if f.pets_allowed is True and self.pets_allowed is False:
            return False
        if f.parking is True and self.parking is False:
            return False
        if f.in_suite_laundry is True and self.in_suite_laundry is False:
            return False
        if f.furnished is True and self.furnished is False:
            return False
        if f.dishwasher is True and self.dishwasher is False:
            return False
        if f.ac is True and self.ac is False:
            return False
        if f.balcony is True and self.balcony is False:
            return False
        if f.gym is True and self.gym is False:
            return False
        if f.move_in_by is not None and self.available_date is not None:
            if self.available_date > f.move_in_by:
                return False
        if include_transit and f.transit_minutes_max is not None:
            if self.transit_minutes is None or self.transit_minutes > f.transit_minutes_max:
                return False
        return True
