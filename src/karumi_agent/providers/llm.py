"""Interchangeable LLM providers.

The agent talks to an LLM through Pipecat's `LLMService` interface, so swapping
provider is configuration rather than a rewrite. This module is the one place
that knows which concrete service to build, and it is deliberately thin: a
registry, a factory, and a clear error when credentials are missing.

The default is Claude Haiku 4.5 — the fastest Claude with dependable tool
calling, which is what a conversational agent needs more than raw capability.
Whether it is *enough* for reasoning over a large `<ui_state>` is an open
question that Phase 2 exists to answer; the point of routing through an
interface is that finding out it isn't would be a config change, not a refactor.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from karumi_agent.observability.logging import get_logger

log = get_logger(__name__)

#: Fast, tool-capable defaults per provider. Overridable with KARUMI_LLM_MODEL.
DEFAULT_MODELS = {
    "anthropic": "claude-haiku-4-5",
    "openai": "gpt-4o-mini",
    "google": "gemini-2.0-flash",
}


class MissingCredentialsError(RuntimeError):
    """Raised when a provider is selected but its API key is not configured."""


def _anthropic(model: str, **kwargs: Any) -> Any:
    from pipecat.services.anthropic.llm import AnthropicLLMService

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise MissingCredentialsError(
            "ANTHROPIC_API_KEY is not set. Add it to .env (see .env.example) or "
            "choose another provider with KARUMI_LLM_PROVIDER."
        )
    return AnthropicLLMService(api_key=api_key, model=model, **kwargs)


def _openai(model: str, **kwargs: Any) -> Any:
    from pipecat.services.openai.llm import OpenAILLMService

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise MissingCredentialsError("OPENAI_API_KEY is not set.")
    return OpenAILLMService(api_key=api_key, model=model, **kwargs)


def _google(model: str, **kwargs: Any) -> Any:
    from pipecat.services.google.llm import GoogleLLMService

    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise MissingCredentialsError("GOOGLE_API_KEY is not set.")
    return GoogleLLMService(api_key=api_key, model=model, **kwargs)


BUILDERS: dict[str, Callable[..., Any]] = {
    "anthropic": _anthropic,
    "openai": _openai,
    "google": _google,
}


def create_llm(
    provider: str | None = None, model: str | None = None, **kwargs: Any
) -> Any:
    """Build the configured LLM service.

    Args:
        provider: Overrides `KARUMI_LLM_PROVIDER` (default: anthropic).
        model: Overrides `KARUMI_LLM_MODEL` (default: the provider's fast model).

    Raises:
        MissingCredentialsError: The provider has no API key configured.
        ValueError: The provider name is not one we know how to build.
    """
    name = (provider or os.environ.get("KARUMI_LLM_PROVIDER") or "anthropic").lower()
    builder = BUILDERS.get(name)
    if builder is None:
        known = ", ".join(sorted(BUILDERS))
        raise ValueError(f"Unknown LLM provider '{name}'. Known providers: {known}.")

    resolved = model or os.environ.get("KARUMI_LLM_MODEL") or DEFAULT_MODELS[name]
    log.info("llm.create", provider=name, model=resolved)
    return builder(resolved, **kwargs)
