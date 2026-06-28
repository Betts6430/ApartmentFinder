"""Parse-logic tests for the Kijiji scraper (scrapers/kijiji.py).

Locks in the trickiest decode quirks: `price.amount` is in cents, `numberbathrooms`
is encoded x10, structured fields live in `attributes.all`, images get up-sized, and
`_split_address` keeps a street only when it contains a digit (private posters hide it).
"""
from __future__ import annotations

from datetime import date

from app.models import PropertyType
from app.scrapers.kijiji import _bigger_image, _parse_listing, _split_address


def _raw(**over) -> dict:
    """A faithful minimal Kijiji RealEstateListing (real field names/shapes)."""
    base = {
        "id": "1732193243",
        "title": "Two Bedroom Two Bath with In-suite Laundry",
        "url": "https://www.kijiji.ca/v-apartments-condos/edmonton/x/1732193243",
        "price": {"amount": 154800},   # cents -> $1548.00
        "imageUrls": ["https://media.kijiji.ca/api/v1/x?rule=kijijica-200-jpg"],
        "description": "Nice place",
        "location": {
            "name": "Central McDougall",
            "address": "10616 84 Avenue NW, Edmonton, AB, T6E 2H3",
            "coordinates": {"latitude": 53.51, "longitude": -113.49},
        },
        "attributes": {"all": [
            {"canonicalName": "numberbedrooms", "canonicalValues": ["2"]},
            {"canonicalName": "numberbathrooms", "canonicalValues": ["15"]},  # x10 -> 1.5
            {"canonicalName": "unittype", "canonicalValues": ["apartment"]},
            {"canonicalName": "laundryinunit", "canonicalValues": ["1"]},
            {"canonicalName": "petsallowed", "canonicalValues": ["limited"]},
            {"canonicalName": "heat", "canonicalValues": ["1"]},
            {"canonicalName": "areainfeet", "canonicalValues": ["850"]},
            {"canonicalName": "dateavailable", "canonicalValues": ["2026-02-25T00:00:00Z"]},
        ]},
    }
    base.update(over)
    return base


class TestParseListing:
    def test_price_is_in_cents(self):
        assert _parse_listing(_raw()).price == 1548.0   # 154800 / 100

    def test_bathrooms_decoded_x10(self):
        l = _parse_listing(_raw())
        assert l.bathrooms == 1.5     # "15" -> 1.5
        assert l.bedrooms == 2.0

    def test_structured_fields(self):
        l = _parse_listing(_raw())
        assert l.source == "kijiji"
        assert l.property_type == PropertyType.APARTMENT
        assert l.sqft == 850
        assert l.in_suite_laundry is True
        assert l.pets_allowed is True            # "limited" -> True
        assert "Heat included" in l.amenities
        assert l.available_date == date(2026, 2, 25)
        assert l.address == "10616 84 Avenue NW"
        assert l.neighborhood == "Central McDougall"

    def test_image_is_upsized(self):
        assert "kijijica-960-jpg" in _parse_listing(_raw()).photos[0]

    def test_skips_null_or_zero_price(self):
        assert _parse_listing(_raw(price={"amount": None})) is None
        assert _parse_listing(_raw(price={"amount": 0})) is None
        assert _parse_listing(_raw(price={})) is None

    def test_skips_listing_without_id(self):
        assert _parse_listing(_raw(id=None)) is None


class TestSplitAddress:
    def test_full_street_address(self):
        street, city, postal = _split_address("10616 84 Avenue NW, Edmonton, AB, T6E 2H3")
        assert street == "10616 84 Avenue NW"
        assert city == "Edmonton"
        assert postal == "T6E 2H3"

    def test_hidden_street_keeps_city_only(self):
        # Private posters hide the street -> the first part is a city, not a street.
        street, city, postal = _split_address("Edmonton, AB, T6L 1B7")
        assert street is None
        assert city == "Edmonton"
        assert postal == "T6L 1B7"

    def test_missing_address(self):
        assert _split_address(None) == (None, "Edmonton", None)


class TestBiggerImage:
    def test_swaps_thumbnail_rule(self):
        assert _bigger_image("x?rule=kijijica-200-jpg") == "x?rule=kijijica-960-jpg"
