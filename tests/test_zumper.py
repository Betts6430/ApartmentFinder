"""Tests for Zumper detail-page phone extraction (scrapers/zumper.py).

Regression coverage for the "sometimes works" bug: the number lives under
`listing_agents` for most listings (we previously only read `agents`).
"""
from __future__ import annotations

from app.scrapers.zumper import _extract_detail_phone


def _state(data=None, active=None):
    return {"detail": {"entity": {"data": data or {}}, "activeListings": active or []}}


def test_reads_listing_agents():
    s = _state({"listing_agents": [{"phone": "(780) 983-4615"}]})
    assert _extract_detail_phone(s) == "(780) 983-4615"


def test_reads_legacy_agents_array():
    s = _state({"agents": [{"phone": "8443266575"}]})
    assert _extract_detail_phone(s) == "8443266575"


def test_listing_agents_preferred_over_other_fields():
    s = _state({"listing_agents": [{"phone": "7805551234"}], "crm_phone": "7809999999"})
    assert _extract_detail_phone(s) == "7805551234"


def test_falls_back_to_crm_phone_at_data_level():
    s = _state({"crm_phone": "7806664444"})
    assert _extract_detail_phone(s) == "7806664444"


def test_falls_back_to_active_listings_crm_phone():
    s = _state(data={}, active=[{"crm_phone": "(587) 906-0621"}])
    assert _extract_detail_phone(s) == "(587) 906-0621"


def test_genuinely_no_number_returns_none():
    s = _state({"listing_agents": [], "agents": None})
    assert _extract_detail_phone(s) is None


def test_handles_malformed_state():
    assert _extract_detail_phone({}) is None
    assert _extract_detail_phone({"detail": {"entity": {"data": {"agents": [None]}}}}) is None
