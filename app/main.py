from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import cache
from app.config import settings
from app.models import PropertyType, SearchFilters, SortBy
from app.services.search import run_search

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    await cache.init_db()
    yield


app = FastAPI(lifespan=lifespan, title="ApartmentFinder")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "property_types": list(PropertyType), "sort_options": list(SortBy)},
    )


@app.post("/search", response_class=HTMLResponse)
async def search(
    request: Request,
    price_min: int | None = Form(None),
    price_max: int | None = Form(None),
    bedrooms_min: int | None = Form(None),
    bathrooms_min: float | None = Form(None),
    sqft_min: int | None = Form(None),
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
    transit_minutes_max: int | None = Form(None),
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
        price_min=price_min,
        price_max=price_max,
        bedrooms_min=bedrooms_min,
        bathrooms_min=bathrooms_min,
        sqft_min=sqft_min,
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
        transit_minutes_max=transit_minutes_max,
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
