"""Тонкая обёртка над Claude API: вызов с обязательным инструментом."""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def make_client(api_key: str):
    import anthropic
    return anthropic.Anthropic(api_key=api_key, max_retries=3)


def call_tool(client, model: str, system: str | None, prompt: str, tool: dict,
              max_tokens: int = 2000) -> dict:
    kwargs = {}
    if system:
        kwargs["system"] = [{"type": "text", "text": system,
                             "cache_control": {"type": "ephemeral"}}]
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        tools=[tool],
        tool_choice={"type": "tool", "name": tool["name"]},
        messages=[{"role": "user", "content": prompt}],
        **kwargs,
    )
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use":
            return dict(block.input)
    raise RuntimeError(f"модель не вызвала инструмент {tool['name']}")
