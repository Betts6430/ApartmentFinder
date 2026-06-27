"""Three scoring axes for ranking listings:

- value_score:    price vs peer-group (same bedrooms). Cheaper than peers => higher.
- location_score: commute time if available, else distance to downtown. Closer => higher.
- niceness_score: amenities + photos. More desirable building => higher.

All scores are normalized roughly to [-1, 2] so they're comparable. The exact
distribution isn't important — only the relative ordering within a result set.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict

from app.models import Listing, SearchFilters, SortBy

# Downtown Edmonton (Churchill Square) — fallback origin for location score
DOWNTOWN_LAT = 53.5444
DOWNTOWN_LNG = -113.4909


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _compute_value_scores(listings: list[Listing]) -> None:
    """For each listing, compute z-score of price within its bedroom peer-group.

    Negative z-score = cheaper than peers, so we flip the sign so value_score
    is "how good a deal is this" (higher = better). Listings without enough
    peers fall back to the overall distribution.
    """
    by_beds: dict[int, list[float]] = defaultdict(list)
    for l in listings:
        by_beds[int(l.bedrooms)].append(l.price)

    bucket_stats: dict[int, tuple[float, float]] = {}
    for beds, prices in by_beds.items():
        if len(prices) >= 5:
            mean = statistics.mean(prices)
            stdev = statistics.pstdev(prices) or 1.0
            bucket_stats[beds] = (mean, stdev)

    all_prices = [l.price for l in listings]
    overall_mean = statistics.mean(all_prices) if all_prices else 0.0
    overall_stdev = (statistics.pstdev(all_prices) or 1.0) if all_prices else 1.0

    for l in listings:
        mean, stdev = bucket_stats.get(int(l.bedrooms), (overall_mean, overall_stdev))
        z = (l.price - mean) / stdev
        # Cheaper than peers -> high score. Clamp to keep extreme outliers from dominating.
        l.value_score = max(-2.0, min(2.0, -z))


def _compute_location_scores(listings: list[Listing], target_set: bool) -> None:
    """If transit_minutes is populated for some listings, use commute time (lower = better).
    Otherwise (or for listings with no transit data), fall back to distance to downtown.
    """
    if target_set and any(l.transit_minutes is not None for l in listings):
        times = [l.transit_minutes for l in listings if l.transit_minutes is not None]
        max_t = max(times) if times else 1.0
        for l in listings:
            if l.transit_minutes is not None:
                # Score in [-1, 1]: 0 min -> +1, max -> -1
                l.location_score = 1.0 - 2.0 * (l.transit_minutes / max_t) if max_t > 0 else 0.0
            else:
                l.location_score = -1.0  # unknown — push to bottom
        return

    # Fallback: distance to downtown
    for l in listings:
        if l.lat is not None and l.lng is not None:
            d = _haversine_km(l.lat, l.lng, DOWNTOWN_LAT, DOWNTOWN_LNG)
            l.location_score = max(-1.0, 1.0 - d / 15.0)  # 0km->+1, 15km+->-1ish
        else:
            l.location_score = -1.0


def _compute_niceness_scores(listings: list[Listing]) -> None:
    """Amenities present + photos. Each True flag contributes; photos give a small log boost."""
    amenity_fields = ("in_suite_laundry", "dishwasher", "ac", "balcony", "gym", "parking", "furnished")
    for l in listings:
        amenity_pts = sum(1.0 for f in amenity_fields if getattr(l, f) is True)
        photo_pts = math.log1p(len(l.photos)) / math.log(10)  # 0 photos -> 0, 9 -> 1
        # Rough scaling so a listing with all amenities + many photos lands ~2.5
        raw = (amenity_pts / len(amenity_fields)) * 2.0 + photo_pts * 0.5
        l.niceness_score = round(raw, 4)


def apply_scores(listings: list[Listing], filters: SearchFilters) -> None:
    """Populate value_score, location_score, niceness_score on each listing in place."""
    if not listings:
        return
    _compute_value_scores(listings)
    _compute_location_scores(listings, target_set=bool(filters.transit_target))
    _compute_niceness_scores(listings)


def sort_listings(listings: list[Listing], sort_by: SortBy) -> list[Listing]:
    if sort_by == SortBy.BEST_VALUE:
        return sorted(listings, key=lambda l: l.value_score or 0.0, reverse=True)
    if sort_by == SortBy.BEST_LOCATION:
        return sorted(listings, key=lambda l: l.location_score or 0.0, reverse=True)
    if sort_by == SortBy.NICEST_PLACES:
        return sorted(listings, key=lambda l: l.niceness_score or 0.0, reverse=True)
    if sort_by == SortBy.PRICE_ASC:
        return sorted(listings, key=lambda l: l.price)
    if sort_by == SortBy.PRICE_DESC:
        return sorted(listings, key=lambda l: l.price, reverse=True)
    if sort_by == SortBy.PRICE_DROP:
        # Biggest recent drop first; listings with no drop sort to the bottom.
        return sorted(
            listings,
            key=lambda l: (l.prev_price - l.price) if l.prev_price else 0.0,
            reverse=True,
        )
    if sort_by == SortBy.NEWEST:
        # Rank by when the listing first appeared in our pool (real recency).
        # scraped_at is near-uniform across a pool, so it's only a tiebreak fallback.
        return sorted(listings, key=lambda l: (l.first_seen or l.scraped_at), reverse=True)
    return listings
