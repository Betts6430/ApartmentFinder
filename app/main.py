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
from app.services.search import run_search

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


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
    listings = await run_search(filters)

    # Source breakdown for the header chip
    source_counts: dict[str, int] = {}
    for l in listings:
        source_counts[l.source] = source_counts.get(l.source, 0) + 1

    return templates.TemplateResponse(
        "results.html",
        {
            "request": request,
            "filters": filters,
            "listings": listings,
            "count": len(listings),
            "source_counts": dict(sorted(source_counts.items(), key=lambda kv: -kv[1])),
            "google_maps_api_key": settings.google_maps_api_key,
        },
    )
