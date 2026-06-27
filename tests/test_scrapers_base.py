"""Tests for the shared scraper helpers (scrapers/base.py)."""
from __future__ import annotations

from app.scrapers.base import normalize_phone, sane_sqft


class TestNormalizePhone:
    def test_plain_ten_digit(self):
        assert normalize_phone("780-555-1234") == ("7805551234", None)

    def test_formatted_with_extension(self):
        # Call-center numbers must keep the extension, not drop it.
        assert normalize_phone("(844) 332-5934 ext. 4525") == ("8443325934", "4525")

    def test_leading_country_code(self):
        assert normalize_phone("1-780-555-1234") == ("17805551234", None)

    def test_too_short_or_missing(self):
        assert normalize_phone("123") == (None, None)
        assert normalize_phone(None) == (None, None)
        assert normalize_phone("") == (None, None)


class TestSaneSqft:
    def test_valid(self):
        assert sane_sqft("850") == 850
        assert sane_sqft(1200) == 1200

    def test_rejects_out_of_range_and_sentinels(self):
        assert sane_sqft(9223372036854775807) is None  # int64 "unknown" sentinel
        assert sane_sqft(1) is None                      # too small
        assert sane_sqft(0) is None
        assert sane_sqft(99999) is None                  # too large

    def test_rejects_junk(self):
        assert sane_sqft(None) is None
        assert sane_sqft("abc") is None
