"""
Provider-agnostic LLM client. Supports Gemini (default) and is structured so
Groq or a local model can be swapped via config (llm.provider).

All providers must return plain text or JSON text. JSON mode is requested where
available; otherwise the caller strips code fences and parses.
"""

import json
import logging
import re
import time
from typing import Optional

logger = logging.getLogger(__name__)

# Gemini's free tier allows only 5 generate_content requests per minute per
# model. The generators legitimately make several calls in a row (a duplicate
# premise or malformed JSON costs a retry, and a single cloud-tick run can
# catch up on more than one overdue slot), so hitting the per-minute cap is
# routine rather than exceptional — before this, a 429 crashed the whole run
# and cost the slot. Waiting the window out is the entire fix.
RATE_LIMIT_ATTEMPTS = 4
RATE_LIMIT_SLEEP_S = 65   # just over the 60s window the per-minute quota uses


class LLMClient:
    def complete(self, prompt: str, temperature: float = 0.9) -> str:
        raise NotImplementedError


class GeminiClient(LLMClient):
    def __init__(self, api_key: str, model: str):
        try:
            import google.generativeai as genai
        except ImportError as e:
            raise ImportError(
                "google-generativeai is not installed. Run: pip install google-generativeai"
            ) from e
        genai.configure(api_key=api_key)
        self._genai = genai
        self._model_name = model

    def complete(self, prompt: str, temperature: float = 0.9) -> str:
        model = self._genai.GenerativeModel(
            self._model_name,
            generation_config=self._genai.types.GenerationConfig(
                temperature=temperature,
                response_mime_type="application/json",
            ),
        )
        for attempt in range(1, RATE_LIMIT_ATTEMPTS + 1):
            try:
                response = model.generate_content(prompt)
                return response.text
            except Exception as e:
                if not _is_rate_limit(e):
                    raise
                # A per-day cap will not clear by waiting a minute — only the
                # per-minute one will, so do not burn the run's clock on it.
                if _is_daily_quota(e):
                    logger.error("Gemini daily free-tier quota is exhausted — not retrying")
                    raise
                if attempt == RATE_LIMIT_ATTEMPTS:
                    raise
                logger.warning(
                    "Gemini per-minute quota hit (attempt %d/%d) — waiting %ds",
                    attempt, RATE_LIMIT_ATTEMPTS, RATE_LIMIT_SLEEP_S,
                )
                time.sleep(RATE_LIMIT_SLEEP_S)
        raise AssertionError("unreachable")


def _is_rate_limit(exc: Exception) -> bool:
    """True for Gemini's 429 quota error. Matched structurally where the
    google.api_core exception type is importable, falling back to the message so
    a dependency reshuffle cannot silently turn a handled 429 into a crash."""
    try:
        from google.api_core.exceptions import ResourceExhausted
        if isinstance(exc, ResourceExhausted):
            return True
    except ImportError:
        pass
    text = str(exc)
    return "429" in text and "quota" in text.lower()


def _is_daily_quota(exc: Exception) -> bool:
    """Distinguish the per-day free-tier cap from the per-minute one. Gemini
    names the quota in the error body, e.g.
    GenerateRequestsPerMinutePerProjectPerModel-FreeTier vs ...PerDay..."""
    text = str(exc).lower()
    return "perday" in text.replace(" ", "") or "per day" in text


def load_client(cfg) -> LLMClient:
    provider = cfg.llm.provider
    if provider == "gemini":
        if not cfg.gemini_api_key:
            raise EnvironmentError(
                "GEMINI_API_KEY is not set. Add it to .env or the environment."
            )
        return GeminiClient(api_key=cfg.gemini_api_key, model=cfg.llm.model)
    raise ValueError(f"Unknown LLM provider: {provider!r}. Supported: gemini")


def extract_json(text: str) -> dict | list:
    """Strip markdown code fences and parse JSON."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return json.loads(text)
