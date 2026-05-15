"""Manage ElevenLabs Conversational AI agents.

The same agent handles voice and SMS — channels are configured in the
ElevenLabs dashboard / via phone-number bindings, not as a separate API.

Usage:
    python scripts/agent.py list
    python scripts/agent.py get   --id agent_abc123
    python scripts/agent.py create --name "Support Bot" --prompt "You are a helpful agent."
    python scripts/agent.py delete --id agent_abc123
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from _client import get_client


def _dump(obj) -> None:
    if hasattr(obj, "model_dump"):
        obj = obj.model_dump()
    print(json.dumps(obj, indent=2, default=str))


def cmd_list(client, _args) -> None:
    agents = client.conversational_ai.agents.list()
    _dump(agents)


def cmd_get(client, args) -> None:
    agent_id = args.id or os.getenv("ELEVENLABS_AGENT_ID")
    if not agent_id:
        sys.exit("Provide --id or set ELEVENLABS_AGENT_ID")
    _dump(client.conversational_ai.agents.get(agent_id=agent_id))


def cmd_create(client, args) -> None:
    conversation_config = {
        "agent": {
            "prompt": {"prompt": args.prompt},
            "first_message": args.first_message,
            "language": args.language,
        },
    }
    agent = client.conversational_ai.agents.create(
        name=args.name,
        conversation_config=conversation_config,
    )
    _dump(agent)


def cmd_delete(client, args) -> None:
    agent_id = args.id or os.getenv("ELEVENLABS_AGENT_ID")
    if not agent_id:
        sys.exit("Provide --id or set ELEVENLABS_AGENT_ID")
    client.conversational_ai.agents.delete(agent_id=agent_id)
    print(f"Deleted agent {agent_id}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage ElevenLabs agents")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="List agents")

    p_get = sub.add_parser("get", help="Get a single agent")
    p_get.add_argument("--id", help="Agent id (defaults to ELEVENLABS_AGENT_ID)")

    p_create = sub.add_parser("create", help="Create a new agent")
    p_create.add_argument("--name", required=True)
    p_create.add_argument(
        "--prompt",
        default="You are a friendly, concise assistant.",
        help="System prompt for the agent",
    )
    p_create.add_argument(
        "--first-message",
        default="Hi! How can I help you today?",
        help="The first thing the agent says",
    )
    p_create.add_argument("--language", default="en")

    p_del = sub.add_parser("delete", help="Delete an agent")
    p_del.add_argument("--id", help="Agent id (defaults to ELEVENLABS_AGENT_ID)")

    args = parser.parse_args()
    client = get_client()

    {
        "list": cmd_list,
        "get": cmd_get,
        "create": cmd_create,
        "delete": cmd_delete,
    }[args.cmd](client, args)


if __name__ == "__main__":
    main()
