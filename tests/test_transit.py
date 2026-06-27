"""Tests for the transit departure-time helper (services/transit.py)."""
from __future__ import annotations

import time
from datetime import datetime

from app.services.transit import _next_weekday_morning_ts


def test_departure_is_future_weekday_at_8am():
    ts = _next_weekday_morning_ts()
    assert ts > time.time()                 # Distance Matrix rejects past times
    dt = datetime.fromtimestamp(ts)
    assert dt.hour == 8 and dt.minute == 0
    assert dt.weekday() < 5                  # Mon–Fri only
