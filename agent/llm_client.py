"""Centralized LLM access for the research agent.

All model calls go through :class:`LLMClient`, which owns timeouts, bounded
retries with exponential backoff, auth-error handling (never retried),
structured-output validation, and run-scoped logging. Secrets are never
logged.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from config.settings import Settings

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """Base class for model-call failures."""


class LLMAuthError(LLMError):
    """Authentication/authorization failure — permanent, never retried."""


class LLMTimeoutError(LLMError):
    """The model did not respond within the configured timeout."""


def _is_auth_error(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(marker in text for marker in (
        "401", "403", "unauthorized", "forbidden",
        "invalid api key", "incorrect api key", "authentication",
    ))


class LLMClient:
    """Thin, safe wrapper around the Ollama-compatible chat endpoint.

    Uses Pydantic AI with a structured ``output_type`` so every response is
    schema-validated; malformed output surfaces as an error the caller can
    handle (retry or controlled fallback).
    """

    def __init__(
        self,
        settings: Settings,
        *,
        system_prompt: str = "",
        output_type: Any = None,
        run_id: str = "",
    ) -> None:
        self.settings = settings
        self.system_prompt = system_prompt
        self.output_type = output_type
        self.run_id = run_id or "-"
        self._agent = None

    def _agent_lazy(self):
        if self._agent is None:
            from pydantic_ai import Agent
            from pydantic_ai.models.openai import OpenAIChatModel
            from pydantic_ai.providers.openai import OpenAIProvider

            provider = OpenAIProvider(
                base_url=self.settings.ollama_base_url.rstrip("/"),
                api_key=self.settings.ollama_api_key,
            )
            model = OpenAIChatModel(self.settings.ollama_model, provider=provider)
            kwargs: dict[str, Any] = {}
            if self.system_prompt:
                kwargs["system_prompt"] = self.system_prompt
            if self.output_type is not None:
                kwargs["output_type"] = self.output_type
            self._agent = Agent(model, **kwargs)
        return self._agent

    async def _single_run(self, prompt: str, timeout_s: float):
        agent = self._agent_lazy()
        try:
            result = await asyncio.wait_for(agent.run(prompt), timeout=timeout_s)
        except asyncio.TimeoutError as exc:
            raise LLMTimeoutError(
                f"LLM call timed out after {timeout_s:.0f}s "
                f"(model {self.settings.ollama_model})") from exc
        except Exception as exc:  # noqa: BLE001 — classify below
            if _is_auth_error(exc):
                raise LLMAuthError(
                    "LLM authentication failed — check OLLAMA_API_KEY "
                    "(never retried).") from exc
            raise
        return result.output

    async def run(self, prompt: str, *, timeout_s: float | None = None):
        """Run one structured model call with bounded retries.

        Raises LLMAuthError (permanent) or LLMError (after retries).
        """
        self.settings.require_live_llm()
        timeout_s = timeout_s or self.settings.llm_timeout_s
        max_retries = self.settings.llm_max_retries
        last: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                out = await self._single_run(prompt, timeout_s)
                log.info("[%s] LLM call ok (attempt %d)", self.run_id, attempt + 1)
                return out
            except LLMAuthError:
                log.error("[%s] LLM auth failure — not retrying", self.run_id)
                raise
            except Exception as exc:  # noqa: BLE001 — bounded retry
                last = exc
                if attempt < max_retries:
                    delay = 2.0 * (2 ** attempt)
                    log.warning("[%s] LLM call failed (attempt %d/%d): %s — "
                                "retrying in %.0fs",
                                self.run_id, attempt + 1, max_retries + 1,
                                exc, delay)
                    await asyncio.sleep(delay)
        raise LLMError(
            f"LLM call failed after {max_retries + 1} attempts: {last}") from last
