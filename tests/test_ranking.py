"""Tests for scoring + sorting (services/ranking.py)."""
from __future__ import annotations

from datetime import datetime

from app.models import SearchFilters, SortBy
from app.services import ranking
from tests.conftest import make_listing


def test_value_score_prefers_cheaper_peers():
    listings = [make_listing(id=str(i), price=p, bedrooms=2)
                for i, p in enumerate([1000, 1500, 1500, 1500, 2500])]
    ranking.apply_scores(listings, SearchFilters())
    cheapest = min(listings, key=lambda l: l.price)
    dearest = max(listings, key=lambda l: l.price)
    assert cheapest.value_score > dearest.value_score


def test_sort_price_asc_desc():
    a = make_listing(id="a", price=900)
    b = make_listing(id="b", price=1800)
    assert [l.id for l in ranking.sort_listings([b, a], SortBy.PRICE_ASC)] == ["a", "b"]
    assert [l.id for l in ranking.sort_listings([a, b], SortBy.PRICE_DESC)] == ["b", "a"]


def test_newest_sorts_by_first_seen_not_scrape_time():
    # Same scraped_at; first_seen is the real recency signal.
    now = datetime(2026, 6, 27, 12, 0, 0)
    older = make_listing(id="older", scraped_at=now, first_seen=datetime(2026, 6, 1))
    newer = make_listing(id="newer", scraped_at=now, first_seen=datetime(2026, 6, 20))
    ordered = ranking.sort_listings([older, newer], SortBy.NEWEST)
    assert [l.id for l in ordered] == ["newer", "older"]


def test_newest_falls_back_to_scraped_at_when_first_seen_missing():
    a = make_listing(id="a", scraped_at=datetime(2026, 6, 10), first_seen=None)
    b = make_listing(id="b", scraped_at=datetime(2026, 6, 20), first_seen=None)
    assert [l.id for l in ranking.sort_listings([a, b], SortBy.NEWEST)] == ["b", "a"]


def test_price_drop_sort_orders_by_largest_reduction():
    a = make_listing(id="a", price=1400, prev_price=1500)   # -100
    b = make_listing(id="b", price=1000, prev_price=1300)   # -300
    c = make_listing(id="c", price=1200, prev_price=None)   # no drop
    ordered = ranking.sort_listings([a, b, c], SortBy.PRICE_DROP)
    assert [l.id for l in ordered][:2] == ["b", "a"]
    assert ordered[-1].id == "c"
