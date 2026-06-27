"""Tests for address normalization and the in-memory filter (models.py)."""
from __future__ import annotations

from app.models import SearchFilters, normalize_address
from tests.conftest import make_listing


class TestNormalizeAddress:
    def test_expands_street_type_and_quadrant(self):
        assert normalize_address("123 PODERSKY ST NW") == "123 Podersky Street NW"

    def test_strips_ordinal_on_numbered_street(self):
        assert normalize_address("37th Avenue") == "37 Avenue"

    def test_keeps_plain_numbers_and_alnum_unit(self):
        assert normalize_address("10309 100 Ave") == "10309 100 Avenue"
        assert normalize_address("101a Jasper") == "101A Jasper"

    def test_strips_trailing_punctuation_in_word_tokens(self):
        # Regression: the else-branch used to Title-case the raw token, leaking the comma.
        assert normalize_address("PODERSKY, Edmonton") == "Podersky Edmonton"

    def test_blank_and_none(self):
        assert normalize_address(None) is None
        assert normalize_address("   ") is None

    def test_runs_as_field_validator(self):
        # The Listing field validator should normalize on construction.
        assert make_listing(address="55 whyte ave nw").address == "55 Whyte Avenue NW"


class TestMatches:
    def test_price_bounds(self):
        l = make_listing(price=1500)
        assert l.matches(SearchFilters(price_max=2000))
        assert not l.matches(SearchFilters(price_max=1000))
        assert l.matches(SearchFilters(price_min=1000))
        assert not l.matches(SearchFilters(price_min=2000))

    def test_bedrooms_and_bathrooms_min(self):
        l = make_listing(bedrooms=2, bathrooms=1)
        assert not l.matches(SearchFilters(bedrooms_min=3))
        assert l.matches(SearchFilters(bedrooms_min=2))
        assert not l.matches(SearchFilters(bathrooms_min=2))

    def test_amenity_unknown_is_not_excluded(self):
        # pets_allowed unknown (None) should pass a "pets required" filter, not fail it.
        assert make_listing(pets_allowed=None).matches(SearchFilters(pets_allowed=True))
        assert not make_listing(pets_allowed=False).matches(SearchFilters(pets_allowed=True))

    def test_transit_toggle(self):
        l = make_listing(transit_minutes=None)
        f = SearchFilters(transit_minutes_max=30)
        assert l.matches(f, include_transit=False)       # skipped before enrichment
        assert not l.matches(f, include_transit=True)    # no time -> excluded
        l.transit_minutes = 20
        assert l.matches(f, include_transit=True)
        l.transit_minutes = 45
        assert not l.matches(f, include_transit=True)
