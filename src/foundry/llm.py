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


def stream(cfg: dict, messages: list[dict], on_delta, *, thinking: bool | None = None,
           max_tokens: int | None = None, timeout: int = 1800, **extra) -> str:
    """Streaming completion. Calls on_delta(kind, text) with kind "reasoning" or "content"
    as tokens arrive; returns the full content."""
    thinking = cfg["model"]["thinking"] if thinking is None else thinking
    body = {
        "model": cfg["model"]["id"],
        "messages": messages,
        "max_tokens": max_tokens or cfg["model"]["max_output"],
        **cfg["model"]["sampling"],
        "chat_template_kwargs": {"enable_thinking": thinking},
        **extra,
        "stream": True,
    }
    req = urllib.request.Request(
        config.base_url(cfg) + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    content = []
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for raw in resp:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            choices = json.loads(data).get("choices") or []
            delta = choices[0].get("delta", {}) if choices else {}
            if delta.get("reasoning"):
                on_delta("reasoning", delta["reasoning"])
            if delta.get("content"):
                content.append(delta["content"])
                on_delta("content", delta["content"])
    return "".join(content)


def ask(cfg: dict, system: str, messages: list[dict], on_delta=None, **kw) -> str:
    """Chat and return only the visible answer (thinking stripped). Streams if on_delta is given."""
    msgs = [{"role": "system", "content": system}, *messages]
    if on_delta:
        return strip_thinking(stream(cfg, msgs, on_delta, **kw))
    resp = chat(cfg, msgs, **kw)
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
