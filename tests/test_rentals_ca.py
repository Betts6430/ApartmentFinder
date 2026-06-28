"""Parse-logic tests for the Rentals.ca scraper (scrapers/rentals_ca.py).

Locks in: range-min taking (rentRange[0] etc.), the `[lng, lat]` coordinate ordering
(easy to swap), `listingType` middle-tier mapping, image-scale preference, pet-option
and parking decoding, and the no-price/no-id skips.
"""
from __future__ import annotations

from app.models import PropertyType
from app.scrapers.rentals_ca import (
    _map_property_type,
    _parking_available,
    _parse_node,
    _pets_from_options,
    _pick_image,
)


def _node(**over) -> dict:
    """A faithful minimal Rentals.ca edge node (real field names/shapes)."""
    base = {
        "id": "cmVudGFsbGlzdGluZzo3MzYwNDg=",
        "rentRange": [1460.0, 1589.0],
        "bedsRange": [1.0, 2.0],
        "bathsRange": [1.0, 2.0],
        "sizeRange": [640.0, 751.0],
        "listingType": "residential:apartment:apartment",
        "rentalListingName": "The Level",
        "path": "edmonton/the-level",
        "rentalListingLocation": [-113.620372, 53.437966],  # [lng, lat]
        "address": {"street": "1104 Windermere Way SW", "postalCode": "T6W 0N8"},
        "contact": {"name": None, "phoneNumber": "(587) 853-5683"},
        "petOptions": ["all"],
        "parking": {"parkingAvailable": None, "parkingTypes": [{"parkingType": "outdoor"}]},
        "images": [{"scales": [{"url": "u-small", "name": "small"}, {"url": "u-large", "name": "large"}]}],
    }
    base.update(over)
    return base


class TestParseNode:
    def test_takes_minimum_of_ranges(self):
        l = _parse_node(_node())
        assert l.source == "rentals.ca"
        assert l.price == 1460.0     # rentRange[0]
        assert l.bedrooms == 1.0     # bedsRange[0]
        assert l.bathrooms == 1.0    # bathsRange[0]
        assert l.sqft == 640         # sizeRange[0]
        assert l.property_type == PropertyType.APARTMENT
        assert l.source_url == "https://rentals.ca/edmonton/the-level"
        assert l.postal_code == "T6W 0N8"

    def test_coordinates_are_lng_lat_order(self):
        # rentalListingLocation = [lng, lat] — must not be swapped.
        l = _parse_node(_node())
        assert l.lng == -113.620372
        assert l.lat == 53.437966

    def test_phone_from_contact(self):
        assert _parse_node(_node()).phone == "5878535683"

    def test_image_prefers_largest_scale(self):
        assert _parse_node(_node()).photos == ["u-large"]

    def test_skips_node_without_price(self):
        assert _parse_node(_node(rentRange=[])) is None
        assert _parse_node(_node(rentRange=[0])) is None

    def test_skips_node_without_id(self):
        assert _parse_node(_node(id=None)) is None


class TestHelpers:
    def test_property_type_uses_middle_tier(self):
        assert _map_property_type("residential:house:detached") == PropertyType.HOUSE
        assert _map_property_type("a:townhouse:b") == PropertyType.TOWNHOUSE
        assert _map_property_type("residential:unknowntype:x") == PropertyType.OTHER
        assert _map_property_type(None) == PropertyType.OTHER

    def test_pets_from_options(self):
        assert _pets_from_options(["all"]) is True
        assert _pets_from_options(["cats"]) is True
        assert _pets_from_options(["none"]) is False
        assert _pets_from_options(["no"]) is False
        assert _pets_from_options(None) is None

    def test_parking_available(self):
        assert _parking_available({"parkingAvailable": True}) is True
        assert _parking_available({"parkingTypes": [{"parkingType": "outdoor"}]}) is True
        assert _parking_available({"parkingAvailable": None, "parkingTypes": []}) is None
        assert _parking_available(None) is None

    def test_pick_image_prefers_large(self):
        imgs = [{"scales": [{"url": "s", "name": "small"}, {"url": "m", "name": "medium"}, {"url": "l", "name": "large"}]}]
        assert _pick_image(imgs) == "l"
        assert _pick_image([]) is None
