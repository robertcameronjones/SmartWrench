"""JSON-on-disk message history.

One file per conversation: ``{root}/{conversation_id}.jsonl``. Each line
is one Turn. Append-only — never rewrite. Easy to tail or grep.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import final

from sms_adapter import HistoryStore, Turn, TurnRole


@final
class _JsonHistoryStore:
    def __init__(self, *, root: Path) -> None:
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, conversation_id: str) -> Path:
        # conversation_id is operator-supplied; never trust it as a path.
        safe = conversation_id.replace("/", "_").replace("..", "_")
        return self._root / f"{safe}.jsonl"

    def append(self, conversation_id: str, turn: Turn) -> None:
        path = self._path(conversation_id)
        record = {
            "role": turn.role.value,
            "text": turn.text,
            "timestamp": turn.timestamp.isoformat(),
            "twilio_sid": turn.twilio_sid,
        }
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def load(self, conversation_id: str) -> tuple[Turn, ...]:
        path = self._path(conversation_id)
        if not path.exists():
            return ()
        out: list[Turn] = []
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                out.append(
                    Turn(
                        role=TurnRole(rec["role"]),
                        text=rec["text"],
                        timestamp=datetime.fromisoformat(rec["timestamp"]),
                        twilio_sid=rec.get("twilio_sid", ""),
                    )
                )
        return tuple(out)


def build_json_history_store(*, root: Path) -> HistoryStore:
    """Build a JSON-file history store rooted at ``root``."""
    return _JsonHistoryStore(root=root)


__all__ = ["build_json_history_store"]
