"""Minimal OpenAI-compatible chat client for the local server (stdlib only)."""

import json
import re
import urllib.request

from . import config


def chat(cfg: dict, messages: list[dict], *, thinking: bool | None = None,
         max_tokens: int | None = None, timeout: int = 1800, **extra) -> dict:
    """One non-streaming completion. Returns the raw response dict."""
    thinking = cfg["model"]["thinking"] if thinking is None else thinking
    body = {
        "model": cfg["model"]["id"],
        "messages": messages,
        "max_tokens": max_tokens or cfg["model"]["max_output"],
        **cfg["model"]["sampling"],
        "chat_template_kwargs": {"enable_thinking": thinking},
        **extra,
    }
    req = urllib.request.Request(
        config.base_url(cfg) + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def ask(cfg: dict, system: str, messages: list[dict], **kw) -> str:
    """Chat and return only the visible answer (thinking stripped)."""
    resp = chat(cfg, [{"role": "system", "content": system}, *messages], **kw)
    return strip_thinking(resp["choices"][0]["message"].get("content") or "")


def strip_thinking(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    if "</think>" in text:  # template already opened the think block
        text = text.split("</think>", 1)[1]
    return text.strip()


def extract_json(text: str):
    """Parse the first JSON object/array in a model answer (tolerates ``` fences)."""
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1)
    start = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
    if start < 0:
        raise ValueError(f"no JSON in model output:\n{text[:500]}")
    return json.JSONDecoder().raw_decode(text[start:])[0]
