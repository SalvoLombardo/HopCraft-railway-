"""
Test per la pipeline Smart Multi-City.

Copertura:
  - Helper puri: _is_valid_route, _leg_dates, _days_per_stop, _season_from_date
  - parse_itineraries (llm/base.py) — puro
  - calculate_area    — DB mockato
  - run_smart_multi   — tutti i layer mockati (DB, LLM, FlightProvider cascade)
"""
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.itinerary_engine import (
    _days_per_stop,
    _is_valid_route,
    _leg_dates,
    _season_from_date,
    run_smart_multi,
)
from app.services.llm.base import SuggestedItinerary, parse_itineraries
from app.services.providers.base import FlightOffer


# ---------------------------------------------------------------------------
# _is_valid_route
# ---------------------------------------------------------------------------

class TestIsValidRoute:

    def test_empty_route(self):
        assert _is_valid_route([], "CTA") is False

    def test_only_origin(self):
        assert _is_valid_route(["CTA"], "CTA") is False

    def test_origin_and_return_no_stops(self):
        # ["CTA", "CTA"] — len < 3 ma anche nessuna tappa intermedia
        assert _is_valid_route(["CTA", "CTA"], "CTA") is False

    def test_wrong_start(self):
        assert _is_valid_route(["FCO", "ATH", "CTA"], "CTA") is False

    def test_wrong_end(self):
        assert _is_valid_route(["CTA", "ATH", "FCO"], "CTA") is False

    def test_duplicate_intermediate(self):
        assert _is_valid_route(["CTA", "ATH", "ATH", "CTA"], "CTA") is False

    def test_valid_route_3_elements(self):
        assert _is_valid_route(["CTA", "ATH", "CTA"], "CTA") is True

    def test_valid_route_4_elements(self):
        assert _is_valid_route(["CTA", "ATH", "BUD", "CTA"], "CTA") is True

    def test_valid_route_5_elements(self):
        assert _is_valid_route(["CTA", "ATH", "SOF", "BUD", "CTA"], "CTA") is True


# ---------------------------------------------------------------------------
# _leg_dates
# ---------------------------------------------------------------------------

class TestLegDates:

    def test_single_leg(self):
        d = date(2026, 6, 1)
        result = _leg_dates(d, 12, 1)
        assert result == [d]

    def test_four_legs_12_days(self):
        d = date(2026, 6, 1)
        result = _leg_dates(d, 12, 4)
        assert len(result) == 4
        assert result[0] == d
        # distanza uniforme: 12 // 4 = 3 giorni tra ogni tratta
        assert (result[1] - result[0]).days == 3
        assert (result[2] - result[1]).days == 3

    def test_first_date_equals_date_from(self):
        d = date(2026, 6, 15)
        result = _leg_dates(d, 10, 3)
        assert result[0] == d

    def test_dates_are_increasing(self):
        d = date(2026, 6, 1)
        result = _leg_dates(d, 20, 4)
        for i in range(len(result) - 1):
            assert result[i] < result[i + 1]


# ---------------------------------------------------------------------------
# _days_per_stop
# ---------------------------------------------------------------------------

class TestDaysPerStop:

    def test_zero_stops(self):
        assert _days_per_stop(12, 0) == []

    def test_equal_distribution(self):
        assert _days_per_stop(12, 3) == [4, 4, 4]

    def test_remainder_distributed_to_first(self):
        # 13 giorni, 3 tappe: 13//3=4, resto 1 → prima tappa prende +1
        assert _days_per_stop(13, 3) == [5, 4, 4]

    def test_single_stop(self):
        assert _days_per_stop(10, 1) == [10]

    def test_sum_equals_total_days(self):
        for days in [7, 12, 15, 20, 25]:
            for stops in [1, 2, 3, 4]:
                result = _days_per_stop(days, stops)
                assert sum(result) == days
                assert len(result) == stops


# ---------------------------------------------------------------------------
# _season_from_date
# ---------------------------------------------------------------------------

class TestSeasonFromDate:

    def test_spring(self):
        for month in [3, 4, 5]:
            assert _season_from_date(date(2026, month, 15)) == "primavera"

    def test_summer(self):
        for month in [6, 7, 8]:
            assert _season_from_date(date(2026, month, 15)) == "estate"

    def test_autumn(self):
        for month in [9, 10, 11]:
            assert _season_from_date(date(2026, month, 15)) == "autunno"

    def test_winter(self):
        for month in [12, 1, 2]:
            assert _season_from_date(date(2026, month, 15)) == "inverno"

    def test_boundary_march_1(self):
        assert _season_from_date(date(2026, 3, 1)) == "primavera"

    def test_boundary_december_31(self):
        assert _season_from_date(date(2026, 12, 31)) == "inverno"


# ---------------------------------------------------------------------------
# parse_itineraries — funzione pura di llm/base.py
# ---------------------------------------------------------------------------

VALID_JSON = """[
  {
    "route": ["CTA", "ATH", "BUD", "CTA"],
    "reasoning": "Rotta balcanica economica",
    "estimated_difficulty": "easy",
    "best_season": ["apr", "mag"]
  }
]"""

MARKDOWN_JSON = f"```json\n{VALID_JSON}\n```"

VALID_JSON_MINIMAL = '[{"route": ["CTA", "ATH", "CTA"]}]'


class TestParseItineraries:

    def test_valid_json_returns_list(self):
        result = parse_itineraries(VALID_JSON)
        assert len(result) == 1
        assert result[0].route == ["CTA", "ATH", "BUD", "CTA"]
        assert result[0].reasoning == "Rotta balcanica economica"
        assert result[0].estimated_difficulty == "easy"
        assert result[0].best_season == ["apr", "mag"]

    def test_markdown_wrapped_json_is_stripped(self):
        result = parse_itineraries(MARKDOWN_JSON)
        assert len(result) == 1
        assert result[0].route == ["CTA", "ATH", "BUD", "CTA"]

    def test_missing_optional_fields_use_defaults(self):
        result = parse_itineraries(VALID_JSON_MINIMAL)
        assert len(result) == 1
        assert result[0].reasoning == ""
        assert result[0].estimated_difficulty == "medium"
        assert result[0].best_season == []

    def test_empty_array_returns_empty_list(self):
        result = parse_itineraries("[]")
        assert result == []

    def test_invalid_json_raises_value_error(self):
        with pytest.raises(ValueError, match="Risposta LLM non valida"):
            parse_itineraries("questo non è JSON {{{")

    def test_missing_route_key_raises_value_error(self):
        with pytest.raises(ValueError, match="Risposta LLM non valida"):
            parse_itineraries('[{"reasoning": "no route here"}]')

    def test_non_list_json_raises_value_error(self):
        with pytest.raises(ValueError, match="Risposta LLM non valida"):
            parse_itineraries('{"route": ["CTA", "ATH", "CTA"]}')


# ---------------------------------------------------------------------------
# Fixture per run_smart_multi
# ---------------------------------------------------------------------------

def _make_db_airport(iata, city, lat, lon):
    a = MagicMock()
    a.iata_code = iata
    a.city = city
    a.country = "Italy"
    a.latitude = lat
    a.longitude = lon
    a.is_active = True
    return a


def _make_area_result(origin="CTA"):
    from app.services.area_calculator import AreaResult, ReachableAirport
    return AreaResult(
        origin_iata=origin,
        radius_km=2000,
        num_stops=2,
        airports=[
            ReachableAirport("ATH", "Athens", "Greece", 37.94, 23.94, 850),
            ReachableAirport("BUD", "Budapest", "Hungary", 47.44, 19.26, 1600),
            ReachableAirport("FCO", "Rome", "Italy", 41.80, 12.24, 480),
        ],
    )


def _make_suggestion(route):
    return SuggestedItinerary(
        route=route,
        reasoning="Test route",
        estimated_difficulty="easy",
        best_season=["giu"],
    )


def _make_offers_for_route(route, price_per_leg=50.0):
    """Crea un FlightOffer per ogni tratta della rotta."""
    offers = []
    for i in range(len(route) - 1):
        offers.append(FlightOffer(
            origin=route[i],
            destination=route[i + 1],
            departure=f"2026-06-{(i+1):02d}T08:00:00",
            price_eur=price_per_leg,
            airline="TestAir",
            direct=True,
            duration_minutes=90,
        ))
    return offers


# ---------------------------------------------------------------------------
# run_smart_multi — pipeline completa mockata
# ---------------------------------------------------------------------------

SMART_PARAMS = dict(
    origin="CTA",
    trip_duration_days=12,
    budget_per_person_eur=300.0,
    travelers=1,
    date_from=date(2026, 6, 1),
    date_to=date(2026, 6, 13),
    direct_only=False,
)


class TestRunSmartMulti:

    async def test_happy_path_returns_ranked_itineraries(self):
        """Percorso felice: LLM propone 2 rotte, entrambe sotto budget → top-2 restituite."""
        suggestions = [
            _make_suggestion(["CTA", "ATH", "CTA"]),
            _make_suggestion(["CTA", "BUD", "CTA"]),
        ]
        # Rotta ATH: 2 tratte × 60€ = 120€; Rotta BUD: 2 tratte × 40€ = 80€
        offers_ath = _make_offers_for_route(["CTA", "ATH", "CTA"], price_per_leg=60.0)
        offers_bud = _make_offers_for_route(["CTA", "BUD", "CTA"], price_per_leg=40.0)

        mock_provider = AsyncMock()
        mock_provider.search_multi_city = AsyncMock(side_effect=[offers_ath, offers_bud])

        session = AsyncMock()

        with patch("app.services.itinerary_engine.calculate_area",
                   new=AsyncMock(return_value=_make_area_result())), \
             patch("app.services.itinerary_engine.generate_with_fallback",
                   new=AsyncMock(return_value=suggestions)), \
             patch("app.services.itinerary_engine.get_providers_in_order",
                   new=AsyncMock(return_value=[("serpapi", mock_provider)])), \
             patch("app.services.itinerary_engine.check_rate_limit",
                   new=AsyncMock(return_value=True)), \
             patch("app.services.itinerary_engine.get_provider_quotas",
                   new=AsyncMock(return_value={"serpapi": 200, "amadeus": 1800})):

            result = await run_smart_multi(session=session, **SMART_PARAMS)

        assert len(result.itineraries) == 2
        # Ordinati per prezzo crescente: BUD (80€) prima di ATH (120€)
        assert result.itineraries[0].total_price_per_person_eur == pytest.approx(80.0)
        assert result.itineraries[1].total_price_per_person_eur == pytest.approx(120.0)
        # rank parte da 1
        assert result.itineraries[0].rank == 1

    async def test_origin_not_found_raises(self):
        """Se calculate_area lancia ValueError (origine non trovata), run_smart_multi rilancia."""
        session = AsyncMock()

        with patch("app.services.itinerary_engine.calculate_area",
                   new=AsyncMock(side_effect=ValueError("Aeroporto 'XYZ' non trovato"))):
            with pytest.raises(ValueError, match="XYZ"):
                await run_smart_multi(session=session, **{**SMART_PARAMS, "origin": "XYZ"})

    async def test_all_itineraries_over_budget_raises(self):
        """Tutte le rotte costano più del budget → ValueError con messaggio 'oltre il budget'."""
        suggestions = [_make_suggestion(["CTA", "ATH", "CTA"])]
        expensive_offers = _make_offers_for_route(["CTA", "ATH", "CTA"], price_per_leg=200.0)

        mock_provider = AsyncMock()
        mock_provider.search_multi_city = AsyncMock(return_value=expensive_offers)

        session = AsyncMock()

        with patch("app.services.itinerary_engine.calculate_area",
                   new=AsyncMock(return_value=_make_area_result())), \
             patch("app.services.itinerary_engine.generate_with_fallback",
                   new=AsyncMock(return_value=suggestions)), \
             patch("app.services.itinerary_engine.get_providers_in_order",
                   new=AsyncMock(return_value=[("serpapi", mock_provider)])), \
             patch("app.services.itinerary_engine.check_rate_limit",
                   new=AsyncMock(return_value=True)), \
             patch("app.services.itinerary_engine.get_provider_quotas",
                   new=AsyncMock(return_value={"serpapi": 200, "amadeus": 1800})):

            with pytest.raises(ValueError, match="oltre il budget"):
                await run_smart_multi(
                    session=session, **{**SMART_PARAMS, "budget_per_person_eur": 50.0}
                )

    async def test_no_flights_found_raises(self):
        """Il provider non trova voli per nessuna rotta → ValueError con 'senza copertura'."""
        suggestions = [_make_suggestion(["CTA", "ATH", "CTA"])]

        mock_provider = AsyncMock()
        mock_provider.search_multi_city = AsyncMock(return_value=[])  # nessun volo

        session = AsyncMock()

        with patch("app.services.itinerary_engine.calculate_area",
                   new=AsyncMock(return_value=_make_area_result())), \
             patch("app.services.itinerary_engine.generate_with_fallback",
                   new=AsyncMock(return_value=suggestions)), \
             patch("app.services.itinerary_engine.get_providers_in_order",
                   new=AsyncMock(return_value=[("serpapi", mock_provider)])), \
             patch("app.services.itinerary_engine.check_rate_limit",
                   new=AsyncMock(return_value=True)), \
             patch("app.services.itinerary_engine.get_provider_quotas",
                   new=AsyncMock(return_value={"serpapi": 200, "amadeus": 1800})):

            with pytest.raises(ValueError, match="senza copertura"):
                await run_smart_multi(session=session, **SMART_PARAMS)

    async def test_all_llm_providers_fail_raises(self):
        """Se generate_with_fallback lancia RuntimeError, run_smart_multi rilancia."""
        session = AsyncMock()

        with patch("app.services.itinerary_engine.calculate_area",
                   new=AsyncMock(return_value=_make_area_result())), \
             patch("app.services.itinerary_engine.get_providers_in_order",
                   new=AsyncMock(return_value=[("serpapi", AsyncMock())])), \
             patch("app.services.itinerary_engine.generate_with_fallback",
                   new=AsyncMock(side_effect=RuntimeError("Tutti i provider LLM hanno fallito."))):

            with pytest.raises(RuntimeError, match="provider LLM"):
                await run_smart_multi(session=session, **SMART_PARAMS)

    async def test_no_flight_providers_fails_fast_before_llm(self):
        """Se nessun flight provider è disponibile, la pipeline si ferma
        prima di chiamare l'LLM (risparmia quota) con un messaggio chiaro."""
        session = AsyncMock()
        llm_mock = AsyncMock()

        with patch("app.services.itinerary_engine.calculate_area",
                   new=AsyncMock(return_value=_make_area_result())), \
             patch("app.services.itinerary_engine.get_providers_in_order",
                   new=AsyncMock(return_value=[])), \
             patch("app.services.itinerary_engine.generate_with_fallback", new=llm_mock):

            with pytest.raises(RuntimeError, match="provider di voli"):
                await run_smart_multi(session=session, **SMART_PARAMS)

        llm_mock.assert_not_awaited()

    async def test_mixed_no_data_and_over_budget_raises(self):
        """Mix di rotte senza copertura e sopra budget → messaggio combinato."""
        suggestions = [
            _make_suggestion(["CTA", "ATH", "CTA"]),  # nessun volo
            _make_suggestion(["CTA", "BUD", "CTA"]),  # sopra budget
        ]
        expensive_offers = _make_offers_for_route(["CTA", "BUD", "CTA"], price_per_leg=200.0)

        call_count = 0

        async def fake_search(legs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return []  # nessun volo per ATH
            return expensive_offers

        mock_provider = AsyncMock()
        mock_provider.search_multi_city = fake_search

        session = AsyncMock()

        with patch("app.services.itinerary_engine.calculate_area",
                   new=AsyncMock(return_value=_make_area_result())), \
             patch("app.services.itinerary_engine.generate_with_fallback",
                   new=AsyncMock(return_value=suggestions)), \
             patch("app.services.itinerary_engine.get_providers_in_order",
                   new=AsyncMock(return_value=[("serpapi", mock_provider)])), \
             patch("app.services.itinerary_engine.check_rate_limit",
                   new=AsyncMock(return_value=True)), \
             patch("app.services.itinerary_engine.get_provider_quotas",
                   new=AsyncMock(return_value={"serpapi": 200, "amadeus": 1800})):

            with pytest.raises(ValueError):
                await run_smart_multi(
                    session=session, **{**SMART_PARAMS, "budget_per_person_eur": 50.0}
                )

    async def test_travelers_multiplies_total_price(self):
        """Il prezzo totale per tutti i viaggiatori = prezzo/persona × viaggiatori."""
        suggestions = [_make_suggestion(["CTA", "ATH", "CTA"])]
        offers = _make_offers_for_route(["CTA", "ATH", "CTA"], price_per_leg=50.0)

        mock_provider = AsyncMock()
        mock_provider.search_multi_city = AsyncMock(return_value=offers)

        session = AsyncMock()

        with patch("app.services.itinerary_engine.calculate_area",
                   new=AsyncMock(return_value=_make_area_result())), \
             patch("app.services.itinerary_engine.generate_with_fallback",
                   new=AsyncMock(return_value=suggestions)), \
             patch("app.services.itinerary_engine.get_providers_in_order",
                   new=AsyncMock(return_value=[("serpapi", mock_provider)])), \
             patch("app.services.itinerary_engine.check_rate_limit",
                   new=AsyncMock(return_value=True)), \
             patch("app.services.itinerary_engine.get_provider_quotas",
                   new=AsyncMock(return_value={"serpapi": 200, "amadeus": 1800})):

            result = await run_smart_multi(session=session, **{**SMART_PARAMS, "travelers": 3})

        itin = result.itineraries[0]
        assert itin.total_price_all_travelers_eur == pytest.approx(
            itin.total_price_per_person_eur * 3
        )
