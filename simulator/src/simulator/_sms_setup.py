"""Build the SMS dependency bundle from environment + project layout.

One job: assemble the live :class:`SmsDeps` (Twilio sender, LLM
completer, JSON history store, JSON routing store, prompt paths) plus
the in-memory :class:`SmsContextRegistry`. Returns ``None, None`` if
the SMS env vars aren't set so the simulator boots fine for voice-only
operators.

Required env vars for SMS (already present in ``sms/.env`` +
``llm/.env`` from the existing standalone tools):
- ``TWILIO_ACCOUNT_SID``      (AC...)
- ``TWILIO_AUTH_TOKEN``
- ``TWILIO_FROM_NUMBER``      (E.164, e.g. +13135551212)
- ``OPENROUTER_API_KEY``      (or whatever provider env LiteLLM needs
                               for the chosen model)

Optional:
- ``LLM_MODEL``      LiteLLM model string. Defaults to
                     ``openrouter/openai/gpt-oss-20b:free`` (the model
                     the operator already verified works).
- ``SMS_DATA_DIR``   Defaults to ``<project_root>/data/sms``.
"""

from __future__ import annotations

import os
from pathlib import Path

import structlog

from prompt_composer import PromptPaths
from sms_adapter import (
    SmsDeps,
    build_json_history_store,
    build_json_routing_store,
    build_litellm_completer,
    build_twilio_sender,
)

from simulator._sms_context_registry import SmsContextRegistry

_log = structlog.get_logger(__name__)


def build_sms_deps(
    *,
    project_root: Path,
) -> tuple[SmsDeps | None, SmsContextRegistry | None]:
    """Compose live :class:`SmsDeps` + a fresh registry, or ``(None, None)``.

    Returns ``(None, None)`` when any required env var is missing — the
    Fire route surfaces a 503 if the operator selects channel=sms in
    that state, but the rest of the simulator (voice) still works.
    """
    account_sid = (os.environ.get("TWILIO_ACCOUNT_SID") or "").strip()
    auth_token = (os.environ.get("TWILIO_AUTH_TOKEN") or "").strip()
    from_number = (os.environ.get("TWILIO_FROM_NUMBER") or "").strip()
    # Default to the model the operator already round-tripped via the
    # llm/ chat CLI yesterday. Overridable with LLM_MODEL=... .
    model = (os.environ.get("LLM_MODEL") or "openrouter/openai/gpt-oss-20b:free").strip()

    missing = [
        name
        for name, value in (
            ("TWILIO_ACCOUNT_SID", account_sid),
            ("TWILIO_AUTH_TOKEN", auth_token),
            ("TWILIO_FROM_NUMBER", from_number),
        )
        if not value
    ]
    if missing:
        _log.warning("simulator.sms.disabled", missing_env=missing)
        return None, None

    data_dir = Path(os.environ.get("SMS_DATA_DIR") or (project_root / "data" / "sms"))
    history_dir = data_dir / "history"
    routing_path = data_dir / "routing.json"
    event_log_path = data_dir / "sms.log"
    history_dir.mkdir(parents=True, exist_ok=True)
    routing_path.parent.mkdir(parents=True, exist_ok=True)

    # ``project_root`` points at the 11Labs/ folder (where the case
    # repo, fixtures, and master-prompt config live). The SMS spot md
    # lives in the sibling sms_adapter/ package one level up.
    workspace_root = project_root.parent
    prompt_paths = PromptPaths(
        system=project_root / "config" / "system-prompt.md",
        voice=project_root / "config" / "voice.md",
        sms=workspace_root / "sms_adapter" / "config" / "sms.md",
    )

    deps = SmsDeps(
        twilio_send=build_twilio_sender(
            account_sid=account_sid,
            auth_token=auth_token,
            from_number=from_number,
        ),
        llm_complete=build_litellm_completer(model=model, event_log_path=event_log_path),
        history=build_json_history_store(root=history_dir),
        routing=build_json_routing_store(path=routing_path),
        prompt_paths=prompt_paths,
        event_log_path=event_log_path,
    )
    registry = SmsContextRegistry()
    _log.info(
        "simulator.sms.enabled",
        from_number=from_number,
        model=model,
        history_dir=str(history_dir),
        routing_path=str(routing_path),
        event_log_path=str(event_log_path),
    )
    return deps, registry


__all__ = ["build_sms_deps"]
