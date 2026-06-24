from __future__ import annotations

from abc import ABC, abstractmethod

from app.models import Listing, SearchFilters


class Scraper(ABC):
    name: str

    @abstractmethod
    async def scrape(self, filters: SearchFilters) -> list[Listing]:
        """Fetch listings from this source that broadly match the filters.

        Implementations may pass through whatever subset of filters the source's
        API supports — final filtering is applied in-memory by the orchestrator.
        """
        ...
