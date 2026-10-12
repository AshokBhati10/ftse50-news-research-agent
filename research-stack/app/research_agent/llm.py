from __future__ import annotations

import json
import re
from typing import Any

import httpx


class LLM:
    """Minimal client for any self-hosted OpenAI-compatible /v1/chat/completions server
    (Ollama, vLLM, llama.cpp server). No paid API involved."""

    def __init__(self, base_url: str, model: str, timeout: float = 300.0) -> None:
        self._http = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout)
        self.model = model

    async def chat(self, system: str, user: str, *, temperature: float = 0.2, json_mode: bool = False) -> str:
        body: dict[str, Any] = {
            "model": self.model,
            "temperature": temperature,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        resp = await self._http.post("/chat/completions", json=body)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    async def chat_json(self, system: str, user: str, *, temperature: float = 0.1) -> dict[str, Any]:
        raw = await self.chat(system, user, temperature=temperature, json_mode=True)
        return parse_json_loose(raw)

    async def close(self) -> None:
        await self._http.aclose()


def parse_json_loose(raw: str) -> dict[str, Any]:
    """Local models sometimes wrap JSON in prose or Markdown code fences; recover the first JSON object."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", raw, flags=re.DOTALL)
    if not m:
        raise ValueError(f"LLM did not return JSON: {raw[:200]!r}")
    return json.loads(m.group(0))
