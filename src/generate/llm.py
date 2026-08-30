"""
Provider-agnostic LLM client. Supports Gemini (default) and is structured so
Groq or a local model can be swapped via config (llm.provider).

All providers must return plain text or JSON text. JSON mode is requested where
available; otherwise the caller strips code fences and parses.
"""

import json
import re
from typing import Optional


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
        response = model.generate_content(prompt)
        return response.text


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
