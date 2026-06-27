"""Shared time helper.

`datetime.utcnow()` is deprecated in Python 3.12+. We still store/compare naive
UTC ISO strings throughout the cache, so this returns a tz-*naive* UTC timestamp
(tz-aware now() with tzinfo stripped) — a drop-in for the old `utcnow()` that keeps
the same string format, so existing cached timestamps stay comparable.
"""
from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)
