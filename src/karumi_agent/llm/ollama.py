"""Local LLM access over Ollama's native API.

We talk to Ollama directly rather than through its OpenAI-compatible surface for
one reason: **schema-constrained decoding**. The ``format`` field accepts a full
JSON Schema and is not exposed on the OpenAI-compatible endpoint, and that
constraint is what makes a small local model reliable enough to drive a browser
(see ``agent/schemas.py``).

Two performance details that are worth more than they look:

* **Warmup.** Measured on this machine: cold prefill 98 tok/s versus 3721 tok/s
  warm, i.e. 11.4 s to first response versus 0.40 s. Warming at startup is not an
  optimisation, it is the difference between usable and not.
* **Prefix stability.** Ollama's KV cache can resume a byte-identical prefix, so
  the system prompt and few-shot examples are assembled once and never
  interpolated with per-turn data. Volatile content goes last.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx

from karumi_agent.config import LLMSettings
from karumi_agent.errors import LLMError
from karumi_agent.logging import get_logger

log = get_logger("llm")


@dataclass(slots=True)
class LLMTiming:
    """Per-request timings, fed into the latency report."""

    ttft_s: float | None = None
    total_s: float = 0.0
    prompt_tokens: int = 0
    eval_tokens: int = 0
    tokens_per_s: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "ttft_s": round(self.ttft_s, 3) if self.ttft_s is not None else None,
            "total_s": round(self.total_s, 3),
            "prompt_tokens": self.prompt_tokens,
            "eval_tokens": self.eval_tokens,
            "tokens_per_s": round(self.tokens_per_s, 1),
        }


@dataclass(slots=True)
class StructuredResponse:
    """A schema-constrained result, plus how long it took."""

    data: dict[str, Any]
    raw: str
    timing: LLMTiming = field(default_factory=LLMTiming)


class OllamaClient:
    """Thin async client for the pieces of Ollama we actually use."""

    def __init__(self, settings: LLMSettings) -> None:
        self._settings = settings
        self._base = settings.native_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(settings.request_timeout_s))

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------- lifecycle
    async def is_available(self) -> bool:
        try:
            response = await self._client.get(f"{self._base}/api/tags", timeout=5.0)
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def installed_models(self) -> list[str]:
        try:
            response = await self._client.get(f"{self._base}/api/tags", timeout=10.0)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise LLMError("Could not list Ollama models", cause=str(exc)) from exc
        return [m.get("name", "") for m in payload.get("models", [])]

    async def warmup(self, model: str, *, system: str = "") -> float:
        """Load the model and pay the cold-prefill cost before anyone is waiting.

        Pass the **real** system prompt. Warming with a throwaway "hi" loads the
        weights but leaves the ~600-token rules-and-examples prefix uncached, so
        the first genuine turn still pays for it: measured here, that was 6.6 s to
        first token instead of 0.6 s. Warming with the actual prefix moves that
        cost to startup, where nobody is waiting on it.

        Returns seconds taken.
        """
        started = time.perf_counter()
        messages = [{"role": "system", "content": system}] if system else []
        messages.append({"role": "user", "content": "Ready?"})
        try:
            await self._client.post(
                f"{self._base}/api/chat",
                json={
                    "model": model,
                    "messages": messages,
                    "stream": False,
                    "keep_alive": self._settings.keep_alive,
                    "options": {"num_ctx": self._settings.num_ctx, "num_predict": 1},
                },
                timeout=self._settings.request_timeout_s,
            )
        except httpx.HTTPError as exc:
            raise LLMError("Model warmup failed", model=model, cause=str(exc)) from exc
        elapsed = time.perf_counter() - started
        log.info("model warm", model=model, seconds=round(elapsed, 2))
        return elapsed

    async def warmup_prefix(self, model: str, *, system: str, user: str) -> float:
        """Prefill an exact prompt prefix so a later matching request resumes it.

        ``num_predict: 1`` means we pay the prefill and almost no decode. Used
        after a navigation, when the page state is new and the next real turn
        would otherwise prefill it cold.
        """
        started = time.perf_counter()
        try:
            await self._client.post(
                f"{self._base}/api/chat",
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "stream": False,
                    "keep_alive": self._settings.keep_alive,
                    "options": {"num_ctx": self._settings.num_ctx, "num_predict": 1},
                    "think": False,
                },
                timeout=self._settings.request_timeout_s,
            )
        except httpx.HTTPError as exc:
            raise LLMError("Prefix warmup failed", model=model, cause=str(exc)) from exc
        return time.perf_counter() - started

    # ---------------------------------------------------------- generation
    async def stream_structured(
        self,
        *,
        model: str,
        system: str,
        user: str,
        schema: dict[str, Any],
        stream_field: str | None = None,
        temperature: float | None = None,
    ) -> AsyncIterator[tuple[str, StructuredResponse | None]]:
        """Stream a schema-constrained response.

        Yields ``(delta, None)`` for each fragment of ``stream_field`` as it is
        decoded, then finally ``("", StructuredResponse)``. Streaming the spoken
        field lets speech synthesis start well before the object is complete.
        """
        body: dict[str, Any] = {
            "model": model,
            # Prefix order is deliberate: stable content first so Ollama's KV
            # cache can resume it, volatile page state last.
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": True,
            "format": schema,
            "keep_alive": self._settings.keep_alive,
            "options": {
                "temperature": (
                    temperature if temperature is not None else self._settings.temperature
                ),
                "num_ctx": self._settings.num_ctx,
            },
        }
        # Reasoning models burn latency we cannot afford on CPU.
        body["think"] = False

        started = time.perf_counter()
        timing = LLMTiming()
        chunks: list[str] = []
        extractor = _StreamingFieldExtractor(stream_field) if stream_field else None

        try:
            async with self._client.stream("POST", f"{self._base}/api/chat", json=body) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    piece = event.get("message", {}).get("content", "")
                    if piece:
                        if timing.ttft_s is None:
                            timing.ttft_s = time.perf_counter() - started
                        chunks.append(piece)
                        if extractor is not None:
                            delta = extractor.feed(piece)
                            if delta:
                                yield delta, None

                    if event.get("done"):
                        timing.total_s = time.perf_counter() - started
                        timing.prompt_tokens = int(event.get("prompt_eval_count", 0))
                        timing.eval_tokens = int(event.get("eval_count", 0))
                        duration = int(event.get("eval_duration", 0))
                        if duration:
                            timing.tokens_per_s = timing.eval_tokens / (duration / 1e9)
        except httpx.HTTPError as exc:
            raise LLMError("Local LLM request failed", model=model, cause=str(exc)) from exc

        raw = "".join(chunks)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            # Schema-constrained output should always parse. If it does not, the
            # caller must treat the turn as failed rather than improvise.
            raise LLMError(
                "Local LLM returned unparseable JSON", model=model, raw=raw[:400], cause=str(exc)
            ) from exc
        if not isinstance(data, dict):
            raise LLMError("Local LLM returned a non-object", model=model, raw=raw[:400])

        yield "", StructuredResponse(data=data, raw=raw, timing=timing)

    async def structured(
        self,
        *,
        model: str,
        system: str,
        user: str,
        schema: dict[str, Any],
        temperature: float | None = None,
    ) -> StructuredResponse:
        """Non-streaming convenience wrapper, used by the eval harness."""
        final: StructuredResponse | None = None
        async for _, response in self.stream_structured(
            model=model, system=system, user=user, schema=schema, temperature=temperature
        ):
            if response is not None:
                final = response
        if final is None:  # pragma: no cover - stream always terminates
            raise LLMError("Local LLM produced no response", model=model)
        return final


class _StreamingFieldExtractor:
    """Pulls one string field's value out of a JSON object as it is decoded.

    Ollama streams raw JSON text, so to speak early we have to read the value of
    ``say`` before the object closes. A tiny state machine is enough here because
    the schema guarantees the shape, and it avoids a partial-JSON dependency.
    """

    def __init__(self, field_name: str) -> None:
        self._needle = f'"{field_name}"'
        self._buffer = ""
        self._in_value = False
        self._done = False
        self._escaped = False

    def feed(self, chunk: str) -> str:
        if self._done:
            return ""
        self._buffer += chunk
        emitted: list[str] = []

        if not self._in_value:
            index = self._buffer.find(self._needle)
            if index == -1:
                # Keep only enough tail to match a key split across chunks.
                self._buffer = self._buffer[-len(self._needle) :]
                return ""
            rest = self._buffer[index + len(self._needle) :]
            quote = rest.find('"')
            if quote == -1:
                return ""
            self._in_value = True
            self._buffer = rest[quote + 1 :]

        # Consume the string body, honouring escapes, stopping at the close quote.
        index = 0
        while index < len(self._buffer):
            char = self._buffer[index]
            if self._escaped:
                emitted.append(_unescape(char))
                self._escaped = False
            elif char == "\\":
                self._escaped = True
            elif char == '"':
                self._done = True
                self._buffer = ""
                return "".join(emitted)
            else:
                emitted.append(char)
            index += 1

        self._buffer = ""
        return "".join(emitted)


def _unescape(char: str) -> str:
    return {"n": "\n", "t": " ", "r": "", '"': '"', "\\": "\\", "/": "/"}.get(char, char)
