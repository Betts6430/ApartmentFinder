"""Tests for cross-source dedupe + address normalization (services/search.py)."""
from __future__ import annotations

from app.services.search import _dedupe_cross_source, _norm_address, _same_place
from tests.conftest import make_listing


def test_norm_address_collapses_variants():
    assert _norm_address("123 Main Street NW") == _norm_address("123 Main St")
    assert _norm_address("10 Oak Avenue") == _norm_address("10 Oak Ave")


def test_same_place_by_proximity():
    a = make_listing(lat=53.5000, lng=-113.5000)
    near = make_listing(lat=53.5008, lng=-113.5000)   # ~90 m
    far = make_listing(lat=53.5100, lng=-113.5000)    # ~1.1 km
    assert _same_place(a, near)
    assert not _same_place(a, far)


def test_same_place_falls_back_to_address_without_coords():
    a = make_listing(address="123 Main St")
    b = make_listing(address="123 Main Street")
    assert _same_place(a, b)
    # No coords and no shared address signal -> not the same.
    assert not _same_place(make_listing(address=None), make_listing(address=None))


def test_dedupe_keeps_richest_copy():
    # Same price/beds/baths, ~same spot. The copy with a phone must win (contactable).
    poor = make_listing(id="poor", lat=53.5, lng=-113.5, photos=["a", "b", "c"])
    rich = make_listing(id="rich", lat=53.5001, lng=-113.5, phone="7805551234")
    out = _dedupe_cross_source([poor, rich])
    assert len(out) == 1
    assert out[0].id == "rich"


def test_dedupe_keeps_distinct_units():
    a = make_listing(id="a", lat=53.50, lng=-113.50)
    b = make_listing(id="b", lat=53.52, lng=-113.50)  # ~2 km away
    assert len(_dedupe_cross_source([a, b])) == 2


def test_dedupe_separates_by_price_beds_baths():
    a = make_listing(id="a", price=1500, lat=53.5, lng=-113.5)
    b = make_listing(id="b", price=1900, lat=53.5, lng=-113.5)  # same spot, diff price
    assert len(_dedupe_cross_source([a, b])) == 2
