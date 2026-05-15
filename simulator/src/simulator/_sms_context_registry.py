"""In-memory ``ContextLookup`` for SMS conversations.

The simulator's Fire button (channel=sms) builds an :class:`SmsContext`
and stashes it here keyed by ``conversation_id``. When an inbound SMS
arrives at the webhook, ``handle_inbound`` calls this registry to
recover the variables (customer / vehicle / dealer / slots) so the
prompt composer can substitute them.

Why in-memory: the simulator and the mounted ``sms.server.app`` run in
the **same process**, so the dict is shared by ordinary closure capture.
If we ever split the webhook into its own process, replace this with a
disk- or DB-backed implementation that conforms to the same callable
shape (``conversation_id -> SmsContext | None``).
"""

from __future__ import annotations

from threading import Lock
from typing import final

from sms_adapter import SmsContext


@final
class SmsContextRegistry:
    """Thread-safe ``conversation_id -> SmsContext`` map.

    Implements the implicit ``ContextLookup`` callable shape (``__call__
    (conversation_id) -> SmsContext | None``) so it can be passed
    directly to ``sms_adapter.handle_inbound``.
    """

    def __init__(self) -> None:
        self._by_conversation: dict[str, SmsContext] = {}
        self._lock = Lock()

    def register(self, ctx: SmsContext) -> None:
        """Stash ``ctx`` under its ``conversation_id``.

        Replaces any existing entry for the same id (caller decides
        whether that's an error — see ``open_conversation``).
        """
        with self._lock:
            self._by_conversation[ctx.conversation_id] = ctx

    def forget(self, conversation_id: str) -> None:
        """Drop the context for ``conversation_id`` if present."""
        with self._lock:
            self._by_conversation.pop(conversation_id, None)

    def __call__(self, conversation_id: str) -> SmsContext | None:
        with self._lock:
            return self._by_conversation.get(conversation_id)


__all__ = ["SmsContextRegistry"]
