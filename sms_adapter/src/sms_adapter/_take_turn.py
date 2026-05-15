"""The single function that runs one turn of an SMS conversation.

This is the only place a system prompt is composed and the only place the
LLM is called. ``open_conversation`` and ``handle_inbound`` route through
it; nothing else does.

If you find yourself wanting to compose a prompt or call the LLM elsewhere
in this package, change this function instead.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from prompt_composer import Channel, build_prompt

if TYPE_CHECKING:  # avoid circular import at runtime
    from sms_adapter import LlmComplete, SmsContext, Turn
    from prompt_composer import PromptPaths


def take_turn(
    *,
    ctx: "SmsContext",
    history: "tuple[Turn, ...]",
    inbound: str | None,
    prompt_paths: "PromptPaths",
    llm_complete: "LlmComplete",
) -> str:
    """Compose the system prompt for ``ctx``, ask the LLM what to say next.

    ``inbound`` is the customer's just-arrived text, or ``None`` for the
    opening turn (where the agent speaks first because the SMS spot md
    says so). When ``inbound`` is provided, the caller is responsible for
    appending it to ``history`` *before* calling here — this function
    treats ``history`` as the complete prior conversation and uses
    ``inbound`` only to skip the open-turn branch.

    Returns the assistant's reply as a string. The caller sends it via
    Twilio and persists it.
    """
    rendered = build_prompt(
        case=_VariablesAdapter(ctx.variables),
        channel=Channel.SMS,
        paths=prompt_paths,
    )

    # On the opening turn there is no inbound and no history. Chat models
    # need a user turn to respond to; system-only produces "Hello?" /
    # waiting noise. Inject a minimal kickoff so the LLM proceeds with
    # the opening message defined in the prompt.
    from sms_adapter import Turn, TurnRole
    from datetime import UTC, datetime

    if inbound is None and not history:
        history = (
            Turn(
                role=TurnRole.USER,
                text="(open conversation)",
                timestamp=datetime.now(UTC),
                twilio_sid="",
            ),
        )

    return llm_complete(system=rendered.text, history=history)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


class _VariablesAdapter:
    """Wrap a flat dict in the ``CaseLike`` Protocol that prompt_composer wants."""

    def __init__(self, variables: dict[str, str]) -> None:
        self._variables = variables

    def to_variables(self) -> dict[str, str]:
        return dict(self._variables)


__all__ = ["take_turn"]
