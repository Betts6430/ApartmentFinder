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


def normalize_phone(v: Any) -> Optional[str]:
    """Reduce a raw contact number to digits only, accepting only plausible NANP
    lengths (10, or 11 with a leading country code). Returns None otherwise — this
    naturally drops junk like call-center numbers carrying an `ext.` (too many
    digits) or partial/garbled values."""
    digits = "".join(c for c in str(v or "") if c.isdigit())
    return digits if len(digits) in (10, 11) else None


class Scraper(ABC):
    name: str

    @abstractmethod
    async def scrape(self, filters: SearchFilters) -> list[Listing]:
        """Fetch listings from this source that broadly match the filters.

        Implementations may pass through whatever subset of filters the source's
        API supports — final filtering is applied in-memory by the orchestrator.
        """
        ...
