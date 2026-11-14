"""LLM + embedding clients for free providers.

- OllamaClient: local models via the Ollama REST API (no key, no cost).
- GroqClient:   Groq free tier through its OpenAI-compatible endpoint.

Both return an LLMResponse so the rest of the system never cares which one ran.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol

import httpx


@dataclass
class LLMResponse:
    text: str
    model: str
    provider: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_s: float = 0.0


class LLMError(RuntimeError):
    pass


class ChatModel(Protocol):
    provider: str
    model: str

    def chat(self, messages: list[dict], temperature: float = 0.1, max_tokens: int = 800) -> LLMResponse: ...


class Embedder(Protocol):
    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class OllamaClient:
    provider = "ollama"

    def __init__(self, model: str, base_url: str, timeout_s: float = 180.0):
        self.model = model
        self._http = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout_s)

    def chat(self, messages: list[dict], temperature: float = 0.1, max_tokens: int = 800) -> LLMResponse:
        start = time.perf_counter()
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        try:
            resp = self._http.post("/api/chat", json=payload)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise LLMError(f"Ollama call failed for model '{self.model}': {exc}") from exc
        data = resp.json()
        return LLMResponse(
            text=data.get("message", {}).get("content", "").strip(),
            model=self.model,
            provider=self.provider,
            prompt_tokens=data.get("prompt_eval_count", 0),
            completion_tokens=data.get("eval_count", 0),
            latency_s=time.perf_counter() - start,
        )


class GroqClient:
    provider = "groq"

    def __init__(self, model: str, base_url: str, api_key: str, timeout_s: float = 60.0):
        if not api_key:
            raise LLMError("FINSIGHT_GROQ_API_KEY is empty. Create a free key at console.groq.com.")
        self.model = model
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout_s,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    def chat(self, messages: list[dict], temperature: float = 0.1, max_tokens: int = 800) -> LLMResponse:
        start = time.perf_counter()
        payload = {"model": self.model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
        try:
            resp = self._http.post("/chat/completions", json=payload)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 401:
                raise LLMError(
                    "Groq rejected the API key (401) - set a valid FINSIGHT_GROQ_API_KEY (free key: console.groq.com)"
                ) from exc
            raise LLMError(f"Groq call failed for model '{self.model}': {exc}") from exc
        data = resp.json()
        usage = data.get("usage", {})
        return LLMResponse(
            text=data["choices"][0]["message"]["content"].strip(),
            model=self.model,
            provider=self.provider,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            latency_s=time.perf_counter() - start,
        )


class OllamaEmbedder:
    """nomic-embed-text expects task prefixes; they measurably improve retrieval."""

    def __init__(self, model: str, base_url: str, timeout_s: float = 120.0, batch_size: int = 32):
        self.model = model
        self.batch_size = batch_size
        self._http = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout_s)
        self._prefixed = "nomic" in model

    def _embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            batch = texts[i : i + self.batch_size]
            try:
                resp = self._http.post("/api/embed", json={"model": self.model, "input": batch})
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                raise LLMError(f"Ollama embedding failed for model '{self.model}': {exc}") from exc
            out.extend(resp.json()["embeddings"])
        return out

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if self._prefixed:
            texts = [f"search_document: {t}" for t in texts]
        return self._embed(texts)

    def embed_query(self, text: str) -> list[float]:
        if self._prefixed:
            text = f"search_query: {text}"
        return self._embed([text])[0]


def build_chat_model(provider: str, model: str, settings) -> ChatModel:
    if provider == "ollama":
        return OllamaClient(model, settings.ollama_url, settings.llm_timeout_s)
    if provider == "groq":
        return GroqClient(model, settings.groq_url, settings.groq_api_key)
    raise LLMError(f"Unknown provider '{provider}'. Use 'ollama' or 'groq'.")
