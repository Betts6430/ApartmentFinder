from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional

from app.models import Listing, SearchFilters


def sane_sqft(v: Any) -> Optional[int]:
    """Normalize a raw square-footage value, rejecting junk: missing/unparseable
    values, placeholder zeros/ones, and out-of-range numbers (some sources emit
    int64 sentinels like 9223372036854775807 for 'unknown'). Returns None when the
    value isn't a believable rental size."""
    if v is None:
        return None
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return None
    return n if 50 <= n <= 50000 else None


def normalize_phone(v: Any) -> tuple[Optional[str], Optional[str]]:
    """Parse a raw contact number into (base_digits, extension).

    The base is a 10-digit NANP number, or 11 digits when carrying a leading `1`
    country code. Any remaining digits are treated as an extension — call-center /
    property-manager numbers commonly look like "(844) 332-5934 ext. 4525" or the
    tel: form "8443325934,4525". Returns (None, None) for anything too short to be
    a phone number."""
    digits = "".join(c for c in str(v or "") if c.isdigit())
    if len(digits) < 10:
        return None, None
    if digits[0] == "1" and len(digits) >= 11:
        base, ext = digits[:11], digits[11:]
    else:
        base, ext = digits[:10], digits[10:]
    return base, (ext or None)


class Scraper(ABC):
    name: str

    @abstractmethod
    async def scrape(self, filters: SearchFilters) -> list[Listing]:
        """Fetch listings from this source that broadly match the filters.

        Implementations may pass through whatever subset of filters the source's
        API supports — final filtering is applied in-memory by the orchestrator.
        """
        ...
