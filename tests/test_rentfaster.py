"""Parse-logic tests for the RentFaster scraper (scrapers/rentfaster.py).

Locks in the decode quirks against regressions: string prices, "studio"/"bachelor"
-> 0 beds, type mapping, link variants, pets from cats/dogs integer counts, and 0/1
amenity flags. (Catches *our* code breaking; the site changing is caught separately by
the scraper-health monitor.)
"""
from __future__ import annotations

from app.models import PropertyType
from app.scrapers.rentfaster import _parse_listing


def _raw(**over) -> dict:
    """A faithful minimal RentFaster map.json listing (real field names/shapes)."""
    base = {
        "ref_id": 334283,
        "price": "950",            # the feed sends price as a string
        "beds": "2",
        "baths": "1",
        "type": "Apartment",       # Title-cased in the feed; parser lowercases
        "link": "/properties/x-334283",
        "thumb2": "https://img.example/x.jpg",
        "phone": "5874122733",
        "cats": 0,
        "dogs": 0,
        "sq_feet": 700,
        "address": "10609 101 St NW",
        "community": "Central McDougall",
        "city": "Edmonton",
        "latitude": 53.55,
        "longitude": -113.49,
        "dishwasher": 0,
        "laundry_in_suite": 0,
        "air_conditioning": 0,
        "parking_available": 0,
        "utilities_included": [],
    }
    base.update(over)
    return base


class TestParseListing:
    def test_basic_fields(self):
        l = _parse_listing(_raw())
        assert l.source == "rentfaster"
        assert l.source_url == "https://www.rentfaster.ca/properties/x-334283"
        assert l.price == 950.0          # parsed from the string "950"
        assert l.bedrooms == 2.0
        assert l.bathrooms == 1.0
        assert l.sqft == 700
        assert l.property_type == PropertyType.APARTMENT
        assert l.phone == "5874122733"
        assert l.lat == 53.55 and l.lng == -113.49
        assert l.neighborhood == "Central McDougall"

    def test_studio_and_bachelor_are_zero_beds(self):
        assert _parse_listing(_raw(beds="studio")).bedrooms == 0.0
        assert _parse_listing(_raw(beds="bachelor")).bedrooms == 0.0
        assert _parse_listing(_raw(beds="")).bedrooms == 0.0

    def test_type_mapping(self):
        assert _parse_listing(_raw(type="Basement")).property_type == PropertyType.BASEMENT
        assert _parse_listing(_raw(type="Main Floor")).property_type == PropertyType.HOUSE
        assert _parse_listing(_raw(type="Duplex")).property_type == PropertyType.HOUSE
        assert _parse_listing(_raw(type="Loft")).property_type == PropertyType.APARTMENT
        assert _parse_listing(_raw(type="Spaceship")).property_type == PropertyType.OTHER

    def test_link_variants(self):
        assert _parse_listing(_raw(link="/properties/a")).source_url == "https://www.rentfaster.ca/properties/a"
        assert _parse_listing(_raw(link="https://x.com/p")).source_url == "https://x.com/p"
        # No usable link -> synthesised from the ref_id.
        assert _parse_listing(_raw(link="")).source_url.endswith("/properties/334283")

    def test_pets_from_cats_dogs_counts(self):
        assert _parse_listing(_raw(cats=0, dogs=0)).pets_allowed is False
        assert _parse_listing(_raw(cats=2, dogs=0)).pets_allowed is True
        assert _parse_listing(_raw(cats=None, dogs=None)).pets_allowed is None  # unknown

    def test_amenity_flags_and_utilities(self):
        l = _parse_listing(_raw(dishwasher=1, laundry_in_suite=1, utilities_included=["Heat", "Water"]))
        assert l.dishwasher is True
        assert l.in_suite_laundry is True
        assert "Heat included" in l.amenities and "Water included" in l.amenities

    def test_skips_listing_without_price(self):
        assert _parse_listing(_raw(price=None)) is None
        assert _parse_listing(_raw(price="0")) is None

    def test_skips_listing_without_id(self):
        assert _parse_listing(_raw(ref_id=None)) is None
