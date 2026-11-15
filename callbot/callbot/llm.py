"""Talks to the AI model. Any 'OpenAI-compatible' provider works (Groq, DeepSeek,
OpenRouter, OpenAI, a local Ollama), so switching model is a change in .env only."""

from __future__ import annotations

import logging
import time

import httpx

log = logging.getLogger(__name__)


class LLMError(Exception):
    pass


class LLM:
    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 60) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> dict:
        """Send the conversation, get back the assistant's message (may contain tool calls)."""
        body: dict = {"model": self.model, "messages": messages, "temperature": 0.3,
                      "max_tokens": 2000}  # includes the model's hidden thinking; stops runaway answers
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        last_err = None
        for attempt in range(3):
            try:
                r = httpx.post(
                    f"{self.base_url}/chat/completions",
                    json=body,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    timeout=self.timeout,
                )
                if r.status_code in (429, 500, 502, 503, 504):
                    last_err = LLMError(f"{r.status_code}: {r.text[:200]}")
                    wait = float(r.headers.get("retry-after", 2 * (attempt + 1)))
                    time.sleep(min(wait, 10))
                    continue
                if r.status_code >= 400:
                    raise LLMError(f"{r.status_code}: {r.text[:500]}")
                return r.json()["choices"][0]["message"]
            except httpx.HTTPError as e:
                last_err = LLMError(str(e))
                time.sleep(2 * (attempt + 1))
        raise last_err or LLMError("unknown error")


def transcribe(audio: bytes, filename: str, base_url: str, api_key: str, model: str) -> str:
    """Turn a voice note into text (Whisper)."""
    r = httpx.post(
        f"{base_url.rstrip('/')}/audio/transcriptions",
        headers={"Authorization": f"Bearer {api_key}"},
        files={"file": (filename, audio)},
        data={"model": model, "response_format": "json"},
        timeout=60,
    )
    r.raise_for_status()
    return (r.json().get("text") or "").strip()
