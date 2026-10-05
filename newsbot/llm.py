"""Тонкая обёртка над Claude API: ответ строго в виде JSON по схеме (structured outputs)."""
from __future__ import annotations

import copy
import json
import logging

log = logging.getLogger(__name__)


def make_client(api_key: str):
    import anthropic
    return anthropic.Anthropic(api_key=api_key, max_retries=3)


def _strict(schema: dict) -> dict:
    """Structured outputs требуют additionalProperties: false у каждого объекта."""
    s = copy.deepcopy(schema)

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                node["additionalProperties"] = False
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(s)
    return s


def call_tool(client, model: str, system: str | None, prompt: str, tool: dict,
              max_tokens: int = 2000) -> dict:
    """Возвращает JSON-ответ модели по схеме tool["input_schema"]."""
    kwargs = {}
    if system:
        kwargs["system"] = [{"type": "text", "text": system,
                             "cache_control": {"type": "ephemeral"}}]
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
        output_config={"format": {"type": "json_schema", "schema": _strict(tool["input_schema"])}},
        **kwargs,
    )
    for block in resp.content:
        if getattr(block, "type", None) == "text" and block.text.strip():
            return json.loads(block.text)
    raise RuntimeError(f"модель не вернула JSON для {tool['name']}")
