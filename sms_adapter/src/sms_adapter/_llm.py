"""LLM completer — adapter onto ``llm.complete``.

Translates our ``Turn`` history into the OpenAI-style message list LiteLLM
wants, calls ``llm.complete``, returns the text. Closes over the model
name so the orchestrators don't carry it.

When ``event_log_path`` is set, every call appends two lines to that
file: the full outbound prompt (``[us->llm]``) and the reply
(``[llm->us]``). Format is the flat ``<ts> [<source>] <content>``
defined in ``_event_log``.
"""

from __future__ import annotations

import json
from pathlib import Path

from llm import complete

from sms_adapter import LlmComplete, Turn
from sms_adapter._event_log import log_event


def build_litellm_completer(
    *,
    model: str,
    event_log_path: Path | None = None,
) -> LlmComplete:
    """Return a callable that runs one non-streaming completion via LiteLLM.

    If ``event_log_path`` is set, each call appends two events to it:
    one for the outbound message array, one for the reply.
    """

    # SMS replies are short and customer-facing; we don't want any
    # provider's hidden "reasoning" mode burning tokens and seconds
    # before the visible answer. OpenRouter's normalized control is
    # `reasoning: { enabled: false }`. Other providers ignore unknown
    # extra_body fields, so this is safe across the board.
    extra_body = {"reasoning": {"enabled": False}}

    def _complete(*, system: str, history: tuple[Turn, ...]) -> str:
        messages: list[dict[str, str]] = [{"role": "system", "content": system}]
        for turn in history:
            messages.append({"role": turn.role.value, "content": turn.text})

        log_event(event_log_path, "us->llm", json.dumps(messages, ensure_ascii=False))

        text, meta = complete(messages=messages, model=model, extra_body=extra_body)

        log_event(
            event_log_path,
            "llm->us",
            json.dumps({"reply": text, "meta": meta}, ensure_ascii=False),
        )

        if meta.get("error"):
            raise RuntimeError(f"LLM error from {model}: {meta['error']}")
        if not text:
            raise RuntimeError(f"LLM {model} returned empty completion (meta={meta!r})")
        return text

    return _complete


__all__ = ["build_litellm_completer"]
