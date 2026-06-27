"""Shared test helpers."""
from __future__ import annotations

from app.models import Listing


def make_listing(**overrides) -> Listing:
    """Build a Listing with sensible defaults; override any field per test."""
    base = dict(
        id="x",
        source="test",
        source_url="http://example.com/x",
        title="Test rental",
        price=1500.0,
        bedrooms=2.0,
        bathrooms=1.0,
    )
    base.update(overrides)
    return Listing(**base)
