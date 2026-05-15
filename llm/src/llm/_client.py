"""Thin LiteLLM completion wrapper.

One function: ``complete(messages, model)`` returns the assistant text plus
a metadata dict. No streaming, no printing — that's a CLI concern. The
SMS adapter wants the whole reply as a string in one call; the CLI wants
streaming. They share nothing except this client.
"""

from __future__ import annotations

import time
from typing import TypedDict


class CompletionMeta(TypedDict, total=False):
    """Per-call metadata for logging / debugging."""

    model: str
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    cost: float | None
    elapsed_ms: int
    error: str


def complete(
    *,
    messages: list[dict[str, str]],
    model: str,
    extra_body: dict | None = None,
) -> tuple[str, CompletionMeta]:
    """Run one non-streaming completion. Returns (assistant_text, meta).

    ``messages`` is the standard OpenAI-style list of role/content dicts,
    starting with the system message. ``model`` is any LiteLLM model
    string (e.g. ``"openrouter/openai/gpt-oss-20b:free"``,
    ``"anthropic/claude-3-5-sonnet-20241022"``).

    ``extra_body`` is forwarded to LiteLLM and onto the provider as
    additional JSON body fields. Used for provider-specific switches
    that aren't part of the OpenAI schema — e.g. Qwen's
    ``{"chat_template_kwargs": {"enable_thinking": False}}`` to skip
    its hidden reasoning tokens.

    On API failure, returns ``("", {"error": str, "elapsed_ms": int})``
    so callers can decide whether to retry or surface the error to the
    customer.
    """
    from litellm import completion

    t0 = time.time()
    try:
        kwargs: dict = {"model": model, "messages": messages}
        if extra_body:
            kwargs["extra_body"] = extra_body
        resp = completion(**kwargs)
    except Exception as exc:
        return "", {
            "model": model,
            "error": f"{type(exc).__name__}: {exc}",
            "elapsed_ms": int((time.time() - t0) * 1000),
        }

    text = ""
    if resp.choices:
        text = resp.choices[0].message.content or ""

    usage = getattr(resp, "usage", None)
    meta: CompletionMeta = {
        "model": model,
        "elapsed_ms": int((time.time() - t0) * 1000),
    }
    if usage is not None:
        meta["prompt_tokens"] = getattr(usage, "prompt_tokens", None)
        meta["completion_tokens"] = getattr(usage, "completion_tokens", None)
        meta["total_tokens"] = getattr(usage, "total_tokens", None)
        cost = getattr(resp, "_hidden_params", {}).get("response_cost")
        if cost is not None:
            meta["cost"] = float(cost)

    return text, meta


__all__ = ["CompletionMeta", "complete"]
