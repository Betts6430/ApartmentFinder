"""Scraper health evaluation.

A 5-source scraper pool's biggest silent failure mode is a site changing its markup
so one scraper quietly returns ~0 listings while the others keep working — the pool
still looks fine, you just stop seeing that source. We record each source's per-scrape
listing count (`cache.scrape_health`) and flag a source whose latest count collapsed
relative to its own recent norm.

Pure functions (no DB / no I/O) so they're trivially unit-testable; `cache.py` supplies
the recorded counts and `main` / `search` consume the report.
"""

from __future__ import annotations

import statistics

OK = "ok"
DOWN = "down"
UNKNOWN = "unknown"

# Need a few prior scrapes before calling a source down (avoids false alarms on a
# fresh install), and "down" = latest collapsed below this fraction of the prior norm.
_MIN_PRIOR = 3
_FRACTION = 0.25


def evaluate_health(
    counts: list[int], *, min_prior: int = _MIN_PRIOR, fraction: float = _FRACTION
) -> str:
    """Classify a source from its recent per-scrape counts, newest-first (``counts[0]``
    is the latest scrape). Returns one of:

    - ``"down"``    — latest is 0, or below ``fraction`` of the median of prior scrapes.
    - ``"unknown"`` — no data recorded yet.
    - ``"ok"``      — healthy, or too little history (< ``min_prior`` priors) to judge.

    Uses the median of *prior* scrapes (not the mean, and excluding the latest) so a
    single fluke scrape doesn't move the baseline much, and a recovered source clears
    immediately.
    """
    if not counts:
        return UNKNOWN
    latest, prior = counts[0], counts[1:]
    if len(prior) < min_prior:
        return OK  # not enough history to make a confident call
    base = statistics.median(prior)
    if base <= 0:
        return OK  # baseline itself ~0 — can't meaningfully judge a "drop"
    return DOWN if (latest == 0 or latest < fraction * base) else OK


def health_report(
    histories: dict[str, list[tuple[int, str]]], known_sources: list[str]
) -> list[dict]:
    """Build a per-source status list for display/logging.

    ``histories`` maps source -> ``[(count, scraped_at), ...]`` newest-first
    (from ``cache.get_recent_scrape_counts``). ``known_sources`` is the registered
    scraper order, so every source is listed (in that order) even before it has any
    recorded history. Each entry: ``{source, status, latest, last_scraped_at, baseline}``.
    """
    report: list[dict] = []
    for src in known_sources:
        hist = histories.get(src) or []
        counts = [c for c, _ in hist]
        report.append(
            {
                "source": src,
                "status": evaluate_health(counts),
                "latest": counts[0] if counts else None,
                "last_scraped_at": hist[0][1] if hist else None,
                "baseline": round(statistics.median(counts[1:])) if len(counts) > 1 else None,
            }
        )
    return report
