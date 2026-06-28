"""Tests for the RentCanada scraper (scrapers/rentcanada.py).

Covers the property-feed `_parse_listing` mapping (price/bed/bath from min-of-range,
amenity-slug → boolean flags, pet/parking policies, HTML-stripped description) and
the lazy detail-page `_extract_detail_phone` (pmPhone preferred over the nested
contacts[].phone).
"""
from __future__ import annotations

from app.models import PropertyType
from app.scrapers.rentcanada import _extract_detail_phone, _parse_listing


def _raw(**over) -> dict:
    """A minimal-but-valid RentCanada search-feed listing dict; override per test."""
    base = {
        "id": 123,
        "minRate": 1500,
        "maxRate": 1800,
        "minBeds": 2,
        "minBaths": 1,
        "minSqFt": "750",
        "propertyType": "apartment",
        "address": "10309 123 Street NW",
        "city": "Edmonton",
        "latitude": 53.5,
        "longitude": -113.5,
        "url": "/edmonton-ab/some-building/123",
        "amenities": [],
        "utilities": [],
        "petPolicies": [],
        "parkingPolicies": [],
        "description": "",
    }
    base.update(over)
    return base


class TestParseListing:
    def test_basic_fields(self):
        l = _parse_listing(_raw())
        assert l is not None
        assert l.source == "rentcanada"
        assert l.source_url == "https://www.rentcanada.com/edmonton-ab/some-building/123"
        assert l.price == 1500.0          # from minRate
        assert l.bedrooms == 2.0          # from minBeds
        assert l.bathrooms == 1.0         # from minBaths
        assert l.sqft == 750              # minSqFt is a string in the feed
        assert l.property_type == PropertyType.APARTMENT
        assert l.lat == 53.5 and l.lng == -113.5
        # No phone in the search feed — resolved lazily later.
        assert l.phone is None

    def test_takes_minimum_of_ranges(self):
        # A property spanning 1–3 beds is surfaced as its starting (min) figures,
        # mirroring rentals_ca.py.
        l = _parse_listing(_raw(minBeds=1, maxBeds=3, minBaths=1, maxBaths=2.5, minRate=1200, maxRate=2400))
        assert l.bedrooms == 1.0
        assert l.bathrooms == 1.0
        assert l.price == 1200.0

    def test_no_price_is_skipped(self):
        assert _parse_listing(_raw(minRate=None)) is None
        assert _parse_listing(_raw(minRate=0)) is None

    def test_missing_id_is_skipped(self):
        assert _parse_listing(_raw(id=None)) is None

    def test_property_type_mapping(self):
        assert _parse_listing(_raw(propertyType="main floor")).property_type == PropertyType.HOUSE
        assert _parse_listing(_raw(propertyType="duplex")).property_type == PropertyType.HOUSE
        assert _parse_listing(_raw(propertyType="basement")).property_type == PropertyType.BASEMENT
        assert _parse_listing(_raw(propertyType="condo")).property_type == PropertyType.CONDO
        assert _parse_listing(_raw(propertyType="weird")).property_type == PropertyType.OTHER

    def test_in_suite_laundry_specific_not_building_laundry(self):
        # Building/shared laundry must NOT set the in-suite flag.
        building = _parse_listing(_raw(amenities=[
            {"name": "Laundry On-site", "value": "laundry-on-site"},
            {"name": "Laundry Facilities", "value": "laundry-facilities"},
        ]))
        assert building.in_suite_laundry is None
        in_suite = _parse_listing(_raw(amenities=[{"name": "Washer In-suite", "value": "washer-in-suite"}]))
        assert in_suite.in_suite_laundry is True

    def test_amenity_flags(self):
        l = _parse_listing(_raw(amenities=[
            {"name": "Dishwasher", "value": "dishwasher"},
            {"name": "Central Air Conditioning", "value": "central-air-conditioning"},
            {"name": "Balconies", "value": "balconies"},
            {"name": "Exercise Room", "value": "exercise-room"},
        ]))
        assert l.dishwasher is True
        assert l.ac is True
        assert l.balcony is True
        assert l.gym is True

    def test_patio_counts_as_balcony(self):
        assert _parse_listing(_raw(amenities=[{"name": "Patio", "value": "patio"}])).balcony is True

    def test_furnished(self):
        assert _parse_listing(_raw(amenities=[{"name": "Furnished", "value": "furnished"}])).furnished is True
        assert _parse_listing(_raw(amenities=[{"name": "Not Furnished", "value": "not-furnished"}])).furnished is False
        assert _parse_listing(_raw()).furnished is None

    def test_pets(self):
        assert _parse_listing(_raw(petPolicies=[{"name": "Cat Friendly", "value": "cat-friendly"}])).pets_allowed is True
        assert _parse_listing(_raw(petPolicies=[{"name": "Pets Not Allowed", "value": "pets-not-allowed"}])).pets_allowed is False
        assert _parse_listing(_raw()).pets_allowed is None

    def test_parking_from_policy_or_amenity(self):
        assert _parse_listing(_raw(parkingPolicies=[{"name": "Underground", "value": "underground"}])).parking is True
        assert _parse_listing(_raw(amenities=[{"name": "Surface Parking", "value": "surface-parking"}])).parking is True
        assert _parse_listing(_raw()).parking is None

    def test_utilities_in_amenities_list(self):
        l = _parse_listing(_raw(utilities=[{"name": "Heat", "value": "heat"}, {"name": "Water", "value": "water"}]))
        assert "Heat included" in l.amenities
        assert "Water included" in l.amenities

    def test_description_html_stripped(self):
        l = _parse_listing(_raw(description="<p><strong>Bright</strong> suite &amp; balcony</p>"))
        assert "<" not in l.description
        assert l.description == "Bright suite & balcony"

    def test_external_absolute_url_passthrough(self):
        l = _parse_listing(_raw(url="https://example.com/x"))
        assert l.source_url == "https://example.com/x"


class TestExtractDetailPhone:
    def test_prefers_pmphone(self):
        data = {"pmPhone": "587-413-0706", "contacts": [{"name": "x", "phone": "7805551234"}]}
        assert _extract_detail_phone(data) == "587-413-0706"

    def test_falls_back_to_contacts(self):
        data = {"pmPhone": None, "contacts": [{"name": "Leasing Team", "phone": "5874169969"}]}
        assert _extract_detail_phone(data) == "5874169969"

    def test_nested_inside_pagedata(self):
        # The real listing is nested under pageData; top-level fields are null.
        data = {
            "pmPhone": None,
            "contacts": None,
            "objects": [{"listing": {"pmPhone": None, "contacts": [{"phone": "5871234567"}]}}],
        }
        assert _extract_detail_phone(data) == "5871234567"

    def test_ignores_null_pmphone_string(self):
        # A non-numeric pmPhone must not win over a real contacts number.
        data = {"pmPhone": "", "contacts": [{"phone": "5870001111"}]}
        assert _extract_detail_phone(data) == "5870001111"

    def test_genuinely_none(self):
        assert _extract_detail_phone({"pmPhone": None, "contacts": []}) is None
        assert _extract_detail_phone({}) is None
        assert _extract_detail_phone([]) is None
