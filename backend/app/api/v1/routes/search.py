import asyncio
from datetime import date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_session
from app.models.schemas import FlightOfferOut, ReverseSearchOut, SmartMultiIn, SmartMultiOut
from app.services.search_engine import reverse_search
from app.services.itinerary_engine import run_smart_multi
from app.utils.user_rate_limit import limit_reverse_search, limit_smart_multi

router = APIRouter()

SessionDep = Annotated[AsyncSession, Depends(get_session)]


"""
Endpoint Reverse Search.

GET /api/v1/search/reverse
  ?destination=CTA
  &date_from=2026-04-01
  &date_to=2026-04-03
  &direct_only=false
  &max_results=50
  &origin_lat=52.3     
  &origin_lon=4.9
  &radius_km=600
"""
@router.get("/reverse", response_model=ReverseSearchOut, dependencies=[Depends(limit_reverse_search)])
async def search_reverse(
    session: SessionDep,
    destination: Annotated[
        str, Query(min_length=3, max_length=3, description="Codice IATA destinazione")
    ],
    date_from: Annotated[date, Query(description="Data partenza minima (YYYY-MM-DD)")],
    date_to: Annotated[date, Query(description="Data partenza massima (YYYY-MM-DD)")],
    direct_only: Annotated[bool, Query(description="Solo voli diretti")] = False,
    max_results: Annotated[int, Query(ge=1, le=200, description="Numero massimo risultati")] = 50,
    origin_lat: Annotated[
        float | None, Query(ge=-90, le=90, description="Latitude of the departure area")
    ] = None,
    origin_lon: Annotated[
        float | None, Query(ge=-180, le=180, description="Longitudine of the departure area")
    ] = None,
    radius_km: Annotated[
        int | None, Query(ge=50, le=5000, description="radius in km from the departure area")
    ] = None,
) -> ReverseSearchOut:
    
    #Validation area -------------------------------------------
    if date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from has to be <= date_to")
    if (date_to - date_from).days > 6:
        raise HTTPException(status_code=422, detail="Max range is 7 days")
    if (origin_lat is None) != (origin_lon is None):
        raise HTTPException(
            status_code=422,
            detail="Both origin_lat and origin_lon must be supplied",
        )
    #Validation area -------------------------------------------


    results, cached, fetched_at, provider_status = await reverse_search(
        session=session,
        destination=destination.upper(),
        date_from=date_from,
        date_to=date_to,
        direct_only=direct_only,
        max_results=max_results,
        origin_lat=origin_lat,
        origin_lon=origin_lon,
        radius_km=radius_km,
    )

    if not results:
        # Distinguish "no flights found" from "no provider available at all":
        # the second case is a temporary service condition, not a 404.
        if provider_status.active_provider == "none":
            raise HTTPException(
                status_code=503,
                detail=(
                    "Nessun provider di voli è al momento disponibile "
                    "(quote mensili esaurite o provider temporaneamente non raggiungibili). "
                    "Riprova più tardi."
                ),
            )
        raise HTTPException(status_code=404, detail=f"No flight find to {destination}")

    offers = [
        FlightOfferOut(
            origin=r["origin"],
            origin_city=r["origin_city"],
            price_eur=r["price_eur"],
            airline=r["airline"],
            departure=datetime.fromisoformat(r["departure"]),
            direct=r["direct"],
            duration_minutes=r["duration_minutes"],
            latitude=r["latitude"],
            longitude=r["longitude"],
        )
        for r in results
    ]

    return ReverseSearchOut(
        destination=destination.upper(),
        results=offers,
        cached=cached,
        fetched_at=fetched_at,
        provider_status=provider_status,
    )


@router.post("/smart-multi", response_model=SmartMultiOut, dependencies=[Depends(limit_smart_multi)])
async def search_smart_multi(
    session: SessionDep,
    body: SmartMultiIn,
) -> SmartMultiOut:
    """
    Smart Multi-City: given origin, duration, budget, and dates, 
    returns the top 5 optimized multi-city itineraries with real prices
    """
    #Validation area -------------------------------------------
    if body.trip_duration_days < 5 or body.trip_duration_days > 25:
        raise HTTPException(status_code=422, detail="trip_duration_days has to be between 5 and 25")
    if body.budget_per_person_eur <= 0:
        raise HTTPException(status_code=422, detail="budget_per_person_eur must be positive")
    if body.travelers < 1:
        raise HTTPException(status_code=422, detail="travelers has to be almost 1")
    if body.date_from >= body.date_to:
        raise HTTPException(status_code=422, detail="date_from has to be < date_to")
    #Validation area -------------------------------------------




    try:
        result = await asyncio.wait_for(
            run_smart_multi(
                session=session,
                origin=body.origin.upper(),
                trip_duration_days=body.trip_duration_days,
                budget_per_person_eur=body.budget_per_person_eur,
                travelers=body.travelers,
                date_from=body.date_from,
                date_to=body.date_to,
                direct_only=body.direct_only,
            ),
            timeout=55,
        )
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=503,
            detail=(
                "La ricerca ha impiegato troppo tempo. "
                "Riprova tra qualche secondo — i provider di voli sono temporaneamente lenti."
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    return result
