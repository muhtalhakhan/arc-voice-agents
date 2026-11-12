"""Intent classification. The LLM only ever picks from the intents the current step allows.

`KeywordClassifier` is deterministic and offline, so tests and CI evals never
flake or cost money. `OpenAIClassifier` is the production path; any output
outside the allowed set collapses to "unclear" and the step graph re-prompts.
"""

from __future__ import annotations

import json
import os
import re
from typing import Protocol

import httpx

from .spec import UNCLEAR


class IntentClassifier(Protocol):
    def classify(self, utterance: str, options: list[str], *, question: str) -> str: ...


KEYWORDS: dict[str, list[str]] = {
    "affirm": ["yes", "yeah", "yep", "correct", "speaking", "that's me", "it is", "sure"],
    "deny": ["no", "nope", "not me", "wrong number", "isn't", "is not"],
    "false_alarm": [
        "false alarm",
        "my fault",
        "set it off",
        "accident",
        "by mistake",
        "cleaner",
        "all fine",
        "all good",
        "it was me",
    ],
    "intruder": [
        "break in",
        "broken",
        "intruder",
        "someone inside",
        "smashed",
        "burglar",
        "stranger",
        "don't know who",
    ],
    "fire": ["fire", "smoke", "flames", "burning"],
    "no_fire": ["no fire", "no smoke", "burnt toast", "toast", "steam", "cooking", "testing"],
    "will_attend": ["i'll go", "i will go", "on my way", "heading over", "i can attend", "i'll attend"],
    "cannot_attend": ["can't go", "cannot go", "can't attend", "away", "abroad", "not able"],
}

# Checked first: more specific phrases win over the generic words inside them.
PRIORITY = ["no_fire", "false_alarm", "cannot_attend", "will_attend", "intruder", "fire", "deny", "affirm"]


def _has(text: str, phrase: str) -> bool:
    return re.search(rf"(?<![a-z']){re.escape(phrase)}(?![a-z'])", text) is not None


class KeywordClassifier:
    def classify(self, utterance: str, options: list[str], *, question: str) -> str:
        text = utterance.lower().replace("’", "'")
        for intent in PRIORITY:
            if intent in options and any(_has(text, p) for p in KEYWORDS.get(intent, [])):
                return intent
        return UNCLEAR


class OpenAIClassifier:
    """Uses any OpenAI-compatible chat completions endpoint with a JSON schema response."""

    def __init__(self, model: str | None = None, base_url: str | None = None, api_key: str | None = None) -> None:
        self.model = model or os.environ.get("ARC_LLM_MODEL", "gpt-4o-mini")
        self.base_url = base_url or os.environ.get("ARC_LLM_BASE_URL", "https://api.openai.com/v1")
        self.api_key = api_key or os.environ["ARC_LLM_API_KEY"]
        self.fallback = KeywordClassifier()

    def classify(self, utterance: str, options: list[str], *, question: str) -> str:
        allowed = [*options, UNCLEAR]
        schema = {
            "name": "intent",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {"intent": {"type": "string", "enum": allowed}},
                "required": ["intent"],
                "additionalProperties": False,
            },
        }
        messages = [
            {
                "role": "system",
                "content": "You classify a caller's reply on an alarm verification call. "
                "Pick exactly one intent. If the reply is ambiguous, pick 'unclear'.",
            },
            {"role": "user", "content": f"Agent asked: {question}\nCaller replied: {utterance}"},
        ]
        try:
            resp = httpx.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.model,
                    "messages": messages,
                    "temperature": 0,
                    "response_format": {"type": "json_schema", "json_schema": schema},
                },
                timeout=4.0,  # a live call cannot wait long; fall back instead
            )
            resp.raise_for_status()
            intent = json.loads(resp.json()["choices"][0]["message"]["content"])["intent"]
        except (httpx.HTTPError, KeyError, ValueError):
            return self.fallback.classify(utterance, options, question=question)
        return intent if intent in allowed else UNCLEAR


def default_classifier() -> IntentClassifier:
    return OpenAIClassifier() if os.environ.get("ARC_LLM_API_KEY") else KeywordClassifier()
