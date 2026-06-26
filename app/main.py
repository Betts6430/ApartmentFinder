from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import cache
from app.config import settings
from app.models import PropertyType, SearchFilters, SortBy
from app.scrapers.base import normalize_phone
from app.scrapers.zumper import fetch_listing_phone
from app.services.search import run_search

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# Listings rendered per results page (full ranked set is sliced server-side).
PAGE_SIZE = 60


@asynccontextmanager
async def lifespan(app: FastAPI):
    await cache.init_db()
    yield


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
        property_types=[PropertyType(pt) for pt in property_types],
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
        sort_by=SortBy(sort_by),
    )
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
    cur_page = min(max(_opt_int(page) or 1, 1), total_pages)
    start = (cur_page - 1) * PAGE_SIZE
    page_listings = listings[start : start + PAGE_SIZE]

    page_ids = [l.id for l in page_listings]
    favorite_ids = await cache.get_favorite_ids()
    new_ids = await cache.get_new_ids(page_ids)

    return templates.TemplateResponse(
        "results.html",
        {
            "request": request,
            "filters": filters,
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
async def listing_phone(listing_id: str) -> HTMLResponse:
    """Lazily resolve a listing's contact number (currently Zumper, whose number
    lives on the per-listing detail page) and return the contact panel HTML to
    swap in. Results — including misses — are cached forever, so repeat clicks
    and re-renders are instant and we never re-hit the source for the same id."""
    found = await cache.get_listings([listing_id])
    if not found:
        return HTMLResponse("Listing not found", status_code=404)
    listing = found[0]

    cached = await cache.get_contact(listing_id)
    if cached is not None:
        phone, ext = cached
    else:
        raw = None
        if listing.source == "zumper":
            raw = await fetch_listing_phone(listing.source_url)
        phone, ext = normalize_phone(raw)
        await cache.save_contact(listing_id, phone, ext)

    return templates.get_template("_contact_panel.html").render(
        phone=phone,
        ext=ext,
        price=listing.price,
        address=listing.address,
        source=listing.source,
        source_url=listing.source_url,
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
