"""Email alerts for saved searches.

A single dispatch point (`dispatch_alerts`) runs whenever the scraped pool is
genuinely refreshed — for each saved search it finds listings that are *new since
that search was last alerted* (non-transit match, mirroring the in-app new-match
count) and emails them, then stamps `last_alerted_at` so they never re-notify.

A lightweight background poller (`alert_poller`) forces periodic re-scrapes so
alerts arrive without the user opening the app. The whole feature no-ops unless
SMTP + a recipient are configured (see `settings.alerts_enabled`).

Transit filters are intentionally ignored when matching (same as the in-app
count): scoring commute times would need per-search geocoding on every poll.
"""
from __future__ import annotations

import asyncio
import logging
import smtplib
from email.message import EmailMessage

from app import cache
from app.config import settings
from app.models import Listing, SearchFilters

log = logging.getLogger(__name__)


async def resolve_recipient() -> str:
    """Where alerts go: the email set on the Settings page (stored in the DB),
    falling back to the `.env` ALERT_EMAIL_TO. Empty string if neither is set."""
    return (await cache.get_meta(cache.ALERT_EMAIL_KEY)) or settings.alert_email_to


def _send_email(to: str, subject: str, body: str) -> None:
    """Blocking SMTP send (STARTTLS). Call via asyncio.to_thread."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.alert_sender
    msg["To"] = to
    msg.set_content(body)
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as s:
        s.starttls()
        if settings.smtp_user:
            s.login(settings.smtp_user, settings.smtp_password)
        s.send_message(msg)


def _format_listing(l: Listing) -> str:
    beds = "studio" if l.bedrooms == 0 else f"{l.bedrooms:g} bd"
    parts = [f"${l.price:,.0f}/mo", beds, f"{l.bathrooms:g} ba"]
    if l.sqft:
        parts.append(f"{l.sqft} sqft")
    where = l.address or "Edmonton"
    return f"• {' · '.join(parts)} — {where} [{l.source}]\n  {l.source_url}"


async def send_test_email() -> tuple[bool, str]:
    """Send a one-off test to the resolved recipient. Returns (ok, detail) where
    detail is the recipient on success or a human-readable reason on failure."""
    if not settings.smtp_configured:
        return False, "Mail sending isn't set up. Add SMTP_HOST / SMTP_USER / SMTP_PASSWORD to .env."
    recipient = await resolve_recipient()
    if not recipient:
        return False, "No alert email set yet. Save one above first."
    subject = "ApartmentFinder test email"
    body = (
        "This is a test from ApartmentFinder.\n\n"
        "If you're reading this, saved-search alerts are wired up correctly and "
        "will arrive here when your saved searches get new matches."
    )
    try:
        await asyncio.to_thread(_send_email, recipient, subject, body)
    except Exception as e:
        log.exception("test email failed")
        return False, f"Send failed: {e}"
    return True, recipient


def _compose(name: str, new: list[Listing]) -> tuple[str, str]:
    n = len(new)
    subject = f"ApartmentFinder: {n} new match{'es' if n != 1 else ''} for “{name}”"
    body = (
        f"Your saved search “{name}” has {n} new "
        f"listing{'s' if n != 1 else ''}:\n\n"
        + "\n\n".join(_format_listing(l) for l in new)
        + "\n\nOpen the app to view, save, or contact them."
    )
    return subject, body


async def dispatch_alerts(pool: list[Listing]) -> None:
    """Check every saved search against a freshly scraped pool and email new matches.

    Safe to call unconditionally — it returns immediately unless alerts are
    configured. Failures are logged, never raised, so a bad SMTP config can't
    break a search.
    """
    if not settings.smtp_configured or not pool:
        return
    recipient = await resolve_recipient()
    if not recipient:
        return  # no Settings-page email yet and no .env fallback
    by_id = {l.id: l for l in pool}
    try:
        saved = await cache.get_saved_searches()
    except Exception:
        log.exception("alert dispatch: could not load saved searches")
        return

    for s in saved:
        try:
            matching = [l for l in pool if l.matches(s["filters"], include_transit=False)]
            since = s["last_alerted_at"] or s["last_viewed_at"]
            new_ids = await cache.list_listing_seen_after([l.id for l in matching], since)
            if not new_ids:
                continue
            new = [by_id[i] for i in new_ids if i in by_id]
            new.sort(key=lambda l: l.price)
            subject, body = _compose(s["name"], new)
            await asyncio.to_thread(_send_email, recipient, subject, body)
            await cache.mark_search_alerted(s["id"])
            log.info("alert sent: %d new for saved search %r", len(new), s["name"])
        except Exception:
            log.exception("alert dispatch failed for saved search %r", s.get("name"))


async def alert_poller() -> None:
    """Background loop: periodically force a pool refresh so alerts fire while the
    user is away. Dispatch itself happens inside run_search after a fresh scrape."""
    # Imported lazily to avoid a circular import (search.py -> alerts.py).
    from app.services.search import run_search

    interval = settings.alert_poll_minutes * 60
    log.info("alert poller started (every %d min)", settings.alert_poll_minutes)
    while True:
        await asyncio.sleep(interval)
        try:
            await run_search(SearchFilters())
        except Exception:
            log.exception("alert poller: scheduled refresh failed")
