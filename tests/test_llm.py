"""
Tests for the Gemini client's retry and model-switching behaviour, against a
fake of the client library. No network, no real API key.

These exist because of a real incident: the library's own hidden retry loop
spent a model's whole daily request allowance on one overloaded call (see
REQUEST_OPTIONS in src/generate/llm.py).
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from src.generate import llm

OVERLOADED = RuntimeError("503 Service Unavailable: The model is overloaded.")
PER_MINUTE = RuntimeError("429 quota exceeded quota_id: "
                          '"GenerateRequestsPerMinutePerProjectPerModel-FreeTier"')
PER_DAY = RuntimeError("429 quota exceeded quota_id: "
                       '"GenerateRequestsPerDayPerProjectPerModel-FreeTier"')


class FakeGenai:
    """behaviour: {model name: list of results, each an Exception or a str}.
    The last entry repeats once the list is used up."""

    def __init__(self, behaviour):
        self.behaviour = {k: list(v) for k, v in behaviour.items()}
        self.calls = []          # (model, request_options) per request actually sent
        self.types = SimpleNamespace(GenerationConfig=lambda **kw: kw)

    def GenerativeModel(self, name, generation_config=None):
        outer = self

        class Model:
            def generate_content(self, prompt, request_options=None):
                outer.calls.append((name, request_options))
                queue = outer.behaviour[name]
                result = queue.pop(0) if len(queue) > 1 else queue[0]
                if isinstance(result, Exception):
                    raise result
                return SimpleNamespace(text=result)

        return Model()


@pytest.fixture
def client(monkeypatch):
    sleeps = []
    monkeypatch.setattr(llm.time, "sleep", sleeps.append)

    def make(behaviour):
        c = llm.GeminiClient.__new__(llm.GeminiClient)
        c._genai = FakeGenai(behaviour)
        c._models = list(behaviour)
        c._idx = 0
        c.sleeps = sleeps
        return c

    return make


def test_library_retries_are_switched_off_on_every_request(client):
    c = client({"a": ["ok"]})
    assert c.complete("p") == "ok"
    (_, opts), = c._genai.calls
    assert opts["retry"] is None        # the hidden loop that burned the daily cap
    assert opts["timeout"] > 0


def test_an_overloaded_model_costs_a_bounded_number_of_requests(client):
    """The incident: one overloaded call must never spend more than a handful
    of requests on a model before moving to the next."""
    c = client({"a": [OVERLOADED], "b": ["from b"]})
    assert c.complete("p") == "from b"
    assert [m for m, _ in c._genai.calls] == ["a"] * llm.TRANSIENT_ATTEMPTS + ["b"]


def test_a_blip_is_retried_on_the_same_model(client):
    c = client({"a": [OVERLOADED, "recovered"], "b": ["unused"]})
    assert c.complete("p") == "recovered"
    assert [m for m, _ in c._genai.calls] == ["a", "a"]


def test_daily_cap_switches_model_immediately_and_stays_switched(client):
    c = client({"a": [PER_DAY], "b": ["from b"]})
    assert c.complete("p") == "from b"
    assert c.complete("p") == "from b"
    assert [m for m, _ in c._genai.calls] == ["a", "b", "b"]
    assert c.sleeps == []               # waiting cannot clear a daily cap


def test_per_minute_limit_waits_out_the_window(client):
    c = client({"a": [PER_MINUTE, "ok"]})
    assert c.complete("p") == "ok"
    assert c.sleeps == [llm.RATE_LIMIT_SLEEP_S]


def test_the_last_models_failure_reaches_the_caller_as_unavailable(client):
    c = client({"a": [PER_DAY], "b": [OVERLOADED]})
    with pytest.raises(RuntimeError) as err:
        c.complete("p")
    assert llm.is_unavailable(err.value)


def test_a_real_error_is_not_retried_or_mistaken_for_unavailability(client):
    bad_key = ValueError("API key not valid")
    c = client({"a": [bad_key], "b": ["unused"]})
    with pytest.raises(ValueError):
        c.complete("p")
    assert len(c._genai.calls) == 1
    assert not llm.is_unavailable(bad_key)


def test_a_timeout_moves_on_without_a_second_try(client):
    timed_out = RuntimeError("504 Deadline expired before operation could complete.")
    c = client({"a": [timed_out], "b": ["from b"]})
    assert c.complete("p") == "from b"
    assert [m for m, _ in c._genai.calls] == ["a", "b"]
