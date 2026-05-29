"""Public API.

Three guarantees:

1. Existing prompt files are read verbatim. The Composer never edits them.
2. One spot md per channel, plus one system prompt per stage family. No fragments.
3. Concat-then-substitute: the stage prompt is concatenated with the
   channel md (separated by a blank line) and then ``{{placeholders}}``
   are resolved against ``case.to_variables()``.

Usage::

    from pathlib import Path
    from prompt_composer import build_prompt, Channel, PromptPaths, PromptStage

    paths = PromptPaths(
        system=Path("11Labs/config/system-prompt.md"),
        post_booking=Path("11Labs/config/prompt-post-booking.md"),
        voice=Path("11Labs/config/voice.md"),
        sms=Path("sms_adapter/config/sms.md"),
    )
    rendered = build_prompt(
        case=case,
        channel=Channel.SMS,
        stage=PromptStage.INITIAL_REMINDER,
        paths=paths,
    )
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol, final, runtime_checkable

from prompt_composer._renderer import (
    MissingPlaceholderError,
    find_placeholders,
    substitute,
)

# Separator between the stage prompt and the channel.md when concatenated.
# Two newlines = a normal markdown paragraph break. We do not inject any text
# (e.g. headers, dividers); that would be Composer "speaking."
_CHANNEL_SEPARATOR = "\n\n"


class Channel(StrEnum):
    """Which channel the rendered prompt is for."""

    VOICE = "voice"
    SMS = "sms"


class PromptStage(StrEnum):
    """Which conversational stage the system prompt is for.

    Values align with ``guidepoint.case.CallStage`` so adapters can pass
    ``PromptStage(stage.value)`` without a dependency on the domain
    package.
    """

    OUTREACH = "outreach"
    INITIAL_REMINDER = "initial_reminder"
    FINAL_REMINDER = "final_reminder"
    FEEDBACK = "feedback"


@runtime_checkable
class CaseLike(Protocol):
    """Anything that can flatten itself into the variables dict.

    In practice this is ``guidepoint.case.Case``. Declared as a protocol
    so the Composer carries no install dependency on the domain package.
    """

    def to_variables(self) -> dict[str, str]: ...


@final
@dataclass(frozen=True, slots=True)
class PromptPaths:
    """Where the spot md files live on disk. Caller-supplied."""

    system: Path
    post_booking: Path
    voice: Path
    sms: Path

    def for_channel(self, channel: Channel) -> Path:
        """Return the channel-specific md path for ``channel``."""
        match channel:
            case Channel.VOICE:
                return self.voice
            case Channel.SMS:
                return self.sms

    def for_stage(self, stage: PromptStage) -> Path:
        """Return the stage system prompt path for ``stage``."""
        if stage is PromptStage.OUTREACH:
            return self.system
        return self.post_booking


@final
@dataclass(frozen=True, slots=True)
class RenderedPrompt:
    """The output of ``build_prompt``.

    ``text`` is what the SMS adapter feeds the LLM as the system message.
    ``variables`` is what the voice adapter hands ElevenLabs as
    ``dynamic_variables``. Both come from the same source so voice and
    SMS cannot disagree about what data was injected.
    """

    text: str
    variables: dict[str, str]
    channel: Channel
    stage: PromptStage
    placeholders_used: frozenset[str]


def build_prompt(
    *,
    case: CaseLike,
    channel: Channel,
    paths: PromptPaths,
    stage: PromptStage = PromptStage.OUTREACH,
) -> RenderedPrompt:
    """Render the full prompt for ``case`` on ``channel`` at ``stage``.

    Reads ``paths.for_stage(stage)`` and ``paths.for_channel(channel)``
    verbatim, concatenates them in that order with a paragraph break, then
    substitutes every ``{{placeholder}}`` using ``case.to_variables()``.
    Raises :class:`MissingPlaceholderError` if any placeholder is
    unresolved.
    """
    stage_text = paths.for_stage(stage).read_text(encoding="utf-8")
    channel_text = paths.for_channel(channel).read_text(encoding="utf-8")
    combined = stage_text + _CHANNEL_SEPARATOR + channel_text

    variables = case.to_variables()
    rendered_text, placeholders_used = substitute(combined, variables)

    return RenderedPrompt(
        text=rendered_text,
        variables=variables,
        channel=channel,
        stage=stage,
        placeholders_used=placeholders_used,
    )


__all__ = [
    "CaseLike",
    "Channel",
    "MissingPlaceholderError",
    "PromptPaths",
    "PromptStage",
    "RenderedPrompt",
    "build_prompt",
    "find_placeholders",
]
