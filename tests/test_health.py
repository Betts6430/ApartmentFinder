"""Tests for scraper-health evaluation (services/health.py)."""
from __future__ import annotations

from app.services.health import DOWN, OK, UNKNOWN, evaluate_health, health_report


class TestEvaluateHealth:
    def test_no_data_is_unknown(self):
        assert evaluate_health([]) == UNKNOWN

    def test_too_little_history_is_ok(self):
        # With fewer than min_prior priors we don't make a call (avoid false alarms).
        assert evaluate_health([500]) == OK
        assert evaluate_health([500, 480, 510]) == OK  # latest + 2 priors

    def test_healthy_latest_is_ok(self):
        assert evaluate_health([500, 480, 510, 495]) == OK

    def test_zero_latest_is_down(self):
        # A source that returned nothing while it used to return hundreds.
        assert evaluate_health([0, 480, 510, 495]) == DOWN

    def test_collapsed_latest_is_down(self):
        # 50 is well under 25% of the ~495 prior norm.
        assert evaluate_health([50, 480, 510, 495]) == DOWN

    def test_fraction_boundary(self):
        # prior median = 400; threshold = 100. 99 -> down, 100 -> ok (not below).
        assert evaluate_health([99, 400, 400, 400]) == DOWN
        assert evaluate_health([100, 400, 400, 400]) == OK

    def test_always_zero_baseline_is_not_flagged(self):
        # If a source has always returned ~0, we can't meaningfully call it "down".
        assert evaluate_health([0, 0, 0, 0]) == OK

    def test_recovers_immediately(self):
        # Median of priors ignores the latest, so a bounce-back clears at once even if
        # the previous scrape was a 0.
        assert evaluate_health([500, 0, 480, 510]) == OK


class TestHealthReport:
    def test_lists_known_sources_in_order_even_without_history(self):
        report = health_report({}, ["rentfaster", "zumper", "kijiji"])
        assert [r["source"] for r in report] == ["rentfaster", "zumper", "kijiji"]
        assert all(r["status"] == UNKNOWN and r["latest"] is None for r in report)

    def test_computes_status_latest_and_baseline(self):
        histories = {
            "rentfaster": [(500, "t3"), (480, "t2"), (510, "t1"), (495, "t0")],
            "zumper": [(0, "t3"), (700, "t2"), (710, "t1"), (690, "t0")],
        }
        report = {r["source"]: r for r in health_report(histories, ["rentfaster", "zumper", "kijiji"])}

        assert report["rentfaster"]["status"] == OK
        assert report["rentfaster"]["latest"] == 500
        assert report["rentfaster"]["baseline"] == 495  # median(480, 510, 495)
        assert report["rentfaster"]["last_scraped_at"] == "t3"

        assert report["zumper"]["status"] == DOWN
        assert report["zumper"]["latest"] == 0
        assert report["zumper"]["baseline"] == 700

        assert report["kijiji"]["status"] == UNKNOWN
        assert report["kijiji"]["latest"] is None
        assert report["kijiji"]["baseline"] is None
