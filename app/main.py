from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import cache
from app.config import settings
from app.models import PropertyType, SearchFilters, SortBy
from app.scrapers.base import normalize_phone
from app.scrapers.zumper import fetch_listing_phone
from app.services.search import filter_and_enrich, run_search

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# Listings rendered per results page (full ranked set is sliced server-side).
PAGE_SIZE = 60


@asynccontextmanager
async def lifespan(app: FastAPI):
    await cache.init_db()
    await cache.prune()  # drop aged-out rows so the DB doesn't grow forever
    poller = None
    if settings.smtp_configured and settings.alert_poll_minutes > 0:
        from app.services.alerts import alert_poller
        poller = asyncio.create_task(alert_poller())
    elif settings.smtp_configured:
        log.info("SMTP configured (poller off: set ALERT_POLL_MINUTES to auto-check)")
    yield
    if poller:
        poller.cancel()


app = FastAPI(lifespan=lifespan, title="ApartmentFinder")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


def _opt_int(v: str | None) -> int | None:
    """HTML forms post empty inputs as "" rather than omitting them, so optional
    numeric fields arrive as strings. Treat blank/whitespace (or unparseable input)
    as a missing value instead of letting it trigger a 422."""
    if v is None or not v.strip():
        return None
    try:
        return int(v)
    except ValueError:
        return None


def _opt_float(v: str | None) -> float | None:
    if v is None or not v.strip():
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _parse_sort(v: str) -> SortBy:
    """Tolerate an unknown sort key instead of 500ing — fall back to Best Value."""
    try:
        return SortBy(v)
    except ValueError:
        return SortBy.BEST_VALUE


def _script_safe_json(s: str) -> str:
    """Escape characters that could break out of a <script> block (or be misread by
    the HTML parser) while keeping the string valid JSON via \\uXXXX escapes."""
    return s.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _parse_property_types(values: list[str]) -> list[PropertyType]:
    """Map form values to PropertyType, silently dropping any unrecognized ones."""
    out: list[PropertyType] = []
    for v in values:
        try:
            out.append(PropertyType(v))
        except ValueError:
            continue
    return out


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "property_types": list(PropertyType),
            "sort_options": list(SortBy),
            "google_maps_api_key": settings.google_maps_api_key,
        },
    )


PLACES_AUTOCOMPLETE_URL = "https://places.googleapis.com/v1/places:autocomplete"
# Bias suggestions toward Edmonton (downtown center, ~50km radius).
_EDMONTON_BIAS = {
    "circle": {"center": {"latitude": 53.5461, "longitude": -113.4938}, "radius": 50000.0}
}


@app.get("/api/places/autocomplete")
async def places_autocomplete(q: str = "") -> JSONResponse:
    """Proxy to Places API (New) so the key stays server-side. Returns plain
    suggestion strings; failures degrade to an empty list (the input still works)."""
    q = q.strip()
    if len(q) < 3 or not settings.google_maps_api_key:
        return JSONResponse({"suggestions": []})
    payload = {"input": q, "includedRegionCodes": ["ca"], "locationBias": _EDMONTON_BIAS}
    headers = {
        "X-Goog-Api-Key": settings.google_maps_api_key,
        "X-Goog-FieldMask": "suggestions.placePrediction.text.text",
    }
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.post(PLACES_AUTOCOMPLETE_URL, json=payload, headers=headers)
        data = r.json()
    except Exception as e:
        log.warning("places autocomplete failed: %s", e)
        return JSONResponse({"suggestions": []})
    suggestions = [
        s["placePrediction"]["text"]["text"]
        for s in data.get("suggestions", [])
        if s.get("placePrediction", {}).get("text", {}).get("text")
    ]
    return JSONResponse({"suggestions": suggestions})


@app.post("/search", response_class=HTMLResponse)
async def search(
    request: Request,
    price_min: str | None = Form(None),
    price_max: str | None = Form(None),
    bedrooms_min: str | None = Form(None),
    bathrooms_min: str | None = Form(None),
    sqft_min: str | None = Form(None),
    property_types: list[str] = Form(default_factory=list),
    pets_allowed: bool = Form(False),
    parking: bool = Form(False),
    in_suite_laundry: bool = Form(False),
    furnished: bool = Form(False),
    dishwasher: bool = Form(False),
    ac: bool = Form(False),
    balcony: bool = Form(False),
    gym: bool = Form(False),
    move_in_by: str | None = Form(None),
    transit_target: str | None = Form(None),
    transit_minutes_max: str | None = Form(None),
    transit_mode: str = Form("transit"),
    sort_by: str = Form(SortBy.BEST_VALUE.value),
    page: str | None = Form(None),
) -> HTMLResponse:
    from datetime import date as _date

    parsed_move_in: _date | None = None
    if move_in_by:
        try:
            parsed_move_in = _date.fromisoformat(move_in_by)
        except ValueError:
            parsed_move_in = None

    filters = SearchFilters(
        price_min=_opt_int(price_min),
        price_max=_opt_int(price_max),
        bedrooms_min=_opt_int(bedrooms_min),
        bathrooms_min=_opt_float(bathrooms_min),
        sqft_min=_opt_int(sqft_min),
        property_types=_parse_property_types(property_types),
        pets_allowed=True if pets_allowed else None,
        parking=True if parking else None,
        in_suite_laundry=True if in_suite_laundry else None,
        furnished=True if furnished else None,
        dishwasher=True if dishwasher else None,
        ac=True if ac else None,
        balcony=True if balcony else None,
        gym=True if gym else None,
        move_in_by=parsed_move_in,
        transit_target=transit_target or None,
        transit_minutes_max=_opt_int(transit_minutes_max),
        transit_mode=transit_mode,
        sort_by=_parse_sort(sort_by),
    )
    return await _run_and_render(request, filters, _opt_int(page) or 1)


async def _run_and_render(request: Request, filters: SearchFilters, page: int) -> HTMLResponse:
    """Run a search and render one paginated page of results. Shared by /search and
    by opening a saved search."""
    result = await run_search(filters)
    listings = result.listings

    # Source breakdown for the header chip (over the full result set, not just the page).
    source_counts: dict[str, int] = {}
    for l in listings:
        source_counts[l.source] = source_counts.get(l.source, 0) + 1

    # Paginate: results are already ranked, so we render one page slice at a time
    # instead of shipping all ~1600 cards (and map markers) in a single response.
    total = len(listings)
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    cur_page = min(max(page, 1), total_pages)
    start = (cur_page - 1) * PAGE_SIZE
    page_listings = listings[start : start + PAGE_SIZE]

    page_ids = [l.id for l in page_listings]
    # Independent lookups — run them concurrently.
    favorite_ids, new_ids = await asyncio.gather(
        cache.get_favorite_ids(), cache.get_new_ids(page_ids)
    )

    return templates.TemplateResponse(
        "results.html",
        {
            "request": request,
            "filters": filters,
            "filters_json": _script_safe_json(filters.model_dump_json()),
            "listings": page_listings,
            "count": total,
            "source_counts": dict(sorted(source_counts.items(), key=lambda kv: -kv[1])),
            "google_maps_api_key": settings.google_maps_api_key,
            "warnings": result.warnings,
            "page": cur_page,
            "total_pages": total_pages,
            "page_size": PAGE_SIZE,
            "page_start": start,
            "page_end": start + len(page_listings),
            "favorite_ids": favorite_ids,
            "new_ids": new_ids,
        },
    )


@app.post("/api/favorites/toggle")
async def toggle_favorite(id: str = Form(...)) -> JSONResponse:
    """Toggle a listing's saved state. Snapshots the listing on save so it
    survives the pool cache expiring or the listing being delisted."""
    if await cache.is_favorite(id):
        await cache.remove_favorite(id)
        return JSONResponse({"favorited": False})
    found = await cache.get_listings([id])
    if not found:
        return JSONResponse({"favorited": False, "error": "listing not found"}, status_code=404)
    await cache.add_favorite(found[0])
    return JSONResponse({"favorited": True})


@app.get("/api/listings/{listing_id}/phone", response_class=HTMLResponse)
async def listing_phone(listing_id: str, align: str = "left") -> HTMLResponse:
    """Lazily resolve a listing's contact number (currently Zumper, whose number
    lives on the per-listing detail page) and return the contact panel HTML to
    swap in. A *definitive* result (number found, or a cleanly-fetched page with no
    number) is cached forever so repeat clicks are instant. A *transient* failure
    (timeout / Cloudflare block) is deliberately NOT cached, so a later click retries
    instead of being stuck on link-out forever."""
    found = await cache.get_listings([listing_id])
    if not found:
        return HTMLResponse("Listing not found", status_code=404)
    listing = found[0]

    cached = await cache.get_contact(listing_id)
    if cached is not None:
        phone, ext = cached
    elif listing.source == "zumper":
        ok, raw = await fetch_listing_phone(listing.source_url)
        phone, ext = normalize_phone(raw)
        if ok:
            await cache.save_contact(listing_id, phone, ext)  # found or genuinely absent
        # else: transient fetch failure — leave uncached so the next click retries.
    else:
        phone, ext = None, None
        await cache.save_contact(listing_id, phone, ext)

    return templates.get_template("_contact_panel.html").render(
        phone=phone,
        ext=ext,
        price=listing.price,
        address=listing.address,
        source=listing.source,
        source_url=listing.source_url,
        align="right" if align == "right" else "left",
    )


@app.get("/favorites", response_class=HTMLResponse)
async def favorites(request: Request) -> HTMLResponse:
    saved = await cache.get_favorites()
    return templates.TemplateResponse(
        "favorites.html",
        {
            "request": request,
            "listings": saved,
            "count": len(saved),
            "favorite_ids": {l.id for l in saved},
            "google_maps_api_key": settings.google_maps_api_key,
        },
    )


def _summarize_filters(f: SearchFilters) -> str:
    """Short human-readable summary of a saved search's filters."""
    parts: list[str] = []
    if f.price_min is not None and f.price_max is not None:
        parts.append(f"${f.price_min:,}–${f.price_max:,}")
    elif f.price_min is not None:
        parts.append(f"≥${f.price_min:,}")
    elif f.price_max is not None:
        parts.append(f"≤${f.price_max:,}")
    if f.bedrooms_min is not None:
        parts.append(f"{f.bedrooms_min}+ bd")
    if f.bathrooms_min is not None:
        parts.append(f"{f.bathrooms_min:g}+ ba")
    if f.sqft_min is not None:
        parts.append(f"{f.sqft_min:,}+ sqft")
    if f.property_types:
        parts.append(", ".join(pt.value.capitalize() for pt in f.property_types))
    amen = [
        name for flag, name in [
            (f.pets_allowed, "Pets"), (f.parking, "Parking"), (f.in_suite_laundry, "Laundry"),
            (f.furnished, "Furnished"), (f.dishwasher, "Dishwasher"), (f.ac, "A/C"),
            (f.balcony, "Balcony"), (f.gym, "Gym"),
        ] if flag
    ]
    if amen:
        parts.append(" · ".join(amen))
    if f.transit_target:
        if f.transit_minutes_max is not None:
            parts.append(f"{f.transit_target} ≤{f.transit_minutes_max} min")
        else:
            parts.append(f"near {f.transit_target}")
    if f.move_in_by is not None:
        parts.append(f"by {f.move_in_by.isoformat()}")
    return " · ".join(parts) if parts else "Any listing"


@app.post("/api/searches")
async def save_search(name: str = Form(...), filters: str = Form(...)) -> JSONResponse:
    """Persist the current search's filters under a name."""
    try:
        sf = SearchFilters.model_validate_json(filters)
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid filters"}, status_code=400)
    clean = name.strip()[:80] or "Untitled search"
    search_id = await cache.add_saved_search(clean, sf)
    return JSONResponse({"ok": True, "id": search_id})


@app.get("/searches", response_class=HTMLResponse)
async def searches(request: Request) -> HTMLResponse:
    """List saved searches with a count of new matches since each was last viewed.

    Match counts apply the full commute filter (via the shared `filter_and_enrich`),
    so they equal what opening the search shows. Commute searches geocode + look up
    transit times here too; results are cached, so repeat loads stay fast."""
    saved = await cache.get_saved_searches()
    pool = await cache.get_cached_search(SearchFilters.scrape_cache_key()) or []

    async def summarize(s: dict) -> dict:
        # mutate=False: concurrent searches must not write transit_minutes onto the
        # shared pool objects (the count only needs the survivor set, not the times).
        matching, _ = await filter_and_enrich(s["filters"], pool, mutate=False)
        new_count = await cache.count_listing_seen_after(
            [l.id for l in matching], s["last_viewed_at"]
        )
        return {
            "id": s["id"],
            "name": s["name"],
            "summary": _summarize_filters(s["filters"]),
            "total": len(matching),
            "new": new_count,
        }

    # Each saved search geocodes + looks up transit independently; run them
    # concurrently so the page is as slow as the slowest one, not their sum.
    items = await asyncio.gather(*(summarize(s) for s in saved))
    return templates.TemplateResponse(
        "searches.html",
        {"request": request, "searches": list(items), "pool_ready": bool(pool)},
    )


@app.get("/searches/{search_id}/open", response_class=HTMLResponse)
async def open_search(request: Request, search_id: int) -> HTMLResponse:
    """Run a saved search and mark it viewed (resetting its new-match count)."""
    s = await cache.get_saved_search(search_id)
    if not s:
        return RedirectResponse("/searches", status_code=303)
    await cache.touch_saved_search(search_id)
    return await _run_and_render(request, s["filters"], 1)


@app.post("/searches/{search_id}/delete")
async def remove_search(search_id: int) -> RedirectResponse:
    await cache.delete_saved_search(search_id)
    return RedirectResponse("/searches", status_code=303)


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


async def _settings_context(request: Request, **extra) -> dict:
    ctx = {
        "request": request,
        "email": await cache.get_meta(cache.ALERT_EMAIL_KEY) or "",
        "smtp_configured": settings.smtp_configured,
        "env_fallback": settings.alert_email_to,
        "poll_minutes": settings.alert_poll_minutes,
    }
    ctx.update(extra)
    return ctx


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, saved: int = 0) -> HTMLResponse:
    return templates.TemplateResponse(
        "settings.html", await _settings_context(request, saved=bool(saved))
    )


@app.post("/settings")
async def save_settings(request: Request, alert_email: str = Form("")) -> Response:
    email = alert_email.strip()
    if email and not _EMAIL_RE.match(email):
        return templates.TemplateResponse(
            "settings.html",
            await _settings_context(request, email=email, error="That doesn't look like a valid email address."),
            status_code=400,
        )
    # Blank clears the override (alerts then fall back to .env ALERT_EMAIL_TO, if any).
    await cache.set_meta(cache.ALERT_EMAIL_KEY, email)
    return RedirectResponse("/settings?saved=1", status_code=303)


@app.post("/api/settings/test-email")
async def test_email() -> JSONResponse:
    """Send a test email to the saved alert recipient; returns {ok, to|error}."""
    from app.services.alerts import send_test_email
    ok, detail = await send_test_email()
    if ok:
        return JSONResponse({"ok": True, "to": detail})
    return JSONResponse({"ok": False, "error": detail}, status_code=400)
