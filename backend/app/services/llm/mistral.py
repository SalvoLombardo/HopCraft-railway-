"""
MistralProvider — Mistral via La Plateforme (fallback volume).

Free tier: 1 miliardo di token/mese.
Nessuna carta di credito. Registrazione su console.mistral.ai.

API OpenAI-compatibile: usa response_format json_object per output strutturato.
"""
import httpx

from app.services.llm.base import (
    LLMProvider,
    SYSTEM_PROMPT,
    SuggestedItinerary,
    build_user_prompt,
    parse_itineraries,
)
from app.utils.http_retry import request_with_retry

_API_URL = "https://api.mistral.ai/v1/chat/completions"
_MODEL = "mistral-small-latest"


class MistralProvider(LLMProvider):

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    async def generate_itineraries(
        self,
        origin: str,
        duration_days: int,
        budget_per_leg: float,
        season: str,
        num_stops: int,
        available_airports: list[str],
        provider_hint: str = "",
    ) -> list[SuggestedItinerary]:
        user_prompt = build_user_prompt(
            origin, duration_days, budget_per_leg, season, num_stops, available_airports,
            provider_hint=provider_hint,
        )

        payload = {
            "model": _MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {"type": "json_object"},
        }

        # Retry only on 5xx: a 429 (rate limit) must fail immediately so the
        # factory falls back to the next LLM provider instead of waiting.
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await request_with_retry(
                lambda: client.post(
                    _API_URL,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json=payload,
                ),
                attempts=2,
                retry_statuses=(500, 502, 503, 504),
                label="Mistral",
            )
            resp.raise_for_status()

        raw = resp.json()["choices"][0]["message"]["content"]
        return parse_itineraries(raw)
