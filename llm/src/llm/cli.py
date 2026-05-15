"""
LLM chat CLI — talk to any model LiteLLM supports, using a file-loaded
system prompt and per-conversation variable substitution.

Design priorities (in order):
  1. Visibility — always show the exact system prompt being sent.
  2. Portability — `--model` switches providers with one flag.
  3. Auditability — every turn logged to JSONL.

Usage:
  python -m llm.cli --prompt prompts/kate.md \\
                 --var dealer_name="Bob's Honda" \\
                 --var vehicle_year=2021 \\
                 --var vehicle_make=Honda \\
                 --var vehicle_model=Civic \\
                 --var service_reason_type="oil change" \\
                 --var slot_options="Tue 8:30am, Wed 11am" \\
                 --var ride_radius_miles=10

Slash commands (typed at the prompt):
  /show          dump the full conversation as it'll be sent
  /system        print the system prompt
  /reset         clear conversation history; keep system prompt
  /model NAME    switch model mid-conversation
  /save PATH     save the conversation to a file
  /help          this list
  /quit          exit (Ctrl-D also works)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv


def load_prompt(path: Path, vars_: dict[str, str]) -> str:
    raw = path.read_text(encoding="utf-8")
    missing = []

    def sub(m: re.Match) -> str:
        key = m.group(1).strip()
        if key in vars_:
            return vars_[key]
        missing.append(key)
        return m.group(0)

    out = re.sub(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}", sub, raw)
    if missing:
        unique = sorted(set(missing))
        print(
            f"\n[warn] {len(unique)} unsubstituted placeholder(s) in prompt: "
            f"{', '.join('{{'+k+'}}' for k in unique)}\n"
            f"       Pass them with --var KEY=VALUE if you want them filled.\n",
            file=sys.stderr,
        )
    return out


def parse_vars(pairs: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in pairs:
        if "=" not in p:
            raise SystemExit(f"--var expects KEY=VALUE, got: {p!r}")
        k, v = p.split("=", 1)
        out[k.strip()] = v
    return out


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def append_log(log_path: Path, record: dict) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def write_transcript_header(path: Path, model: str, system: str,
                            vars_: dict[str, str]) -> None:
    """Write (or append) a session header at the top of the transcript."""
    path.parent.mkdir(parents=True, exist_ok=True)
    sep = "\n\n" + ("=" * 80) + "\n"
    out = [sep, f"# Session started {now_iso()}\n",
           f"- model: `{model}`\n",
           f"- variables: {len(vars_)}\n"]
    if vars_:
        for k, v in vars_.items():
            out.append(f"  - `{k}` = `{v}`\n")
    out.append("\n## System prompt\n\n```\n")
    out.append(system)
    out.append("\n```\n\n## Conversation\n")
    with path.open("a", encoding="utf-8") as f:
        f.write("".join(out))


def append_transcript_turn(path: Path, ts: str, model: str,
                           user: str, assistant: str, meta: dict) -> None:
    cost = meta.get("cost")
    cost_s = f"${cost:.6f}" if isinstance(cost, (int, float)) else "n/a"
    pt = meta.get("prompt_tokens")
    ct = meta.get("completion_tokens")
    elapsed = meta.get("elapsed_ms")
    err = meta.get("error")
    out = [
        f"\n### {ts} — turn\n",
        f"_model: `{model}` · prompt_tokens: {pt} · completion_tokens: {ct} · "
        f"elapsed_ms: {elapsed} · cost: {cost_s}_\n",
    ]
    out.append(f"\n**user**\n```\n{user}\n```\n")
    if err:
        out.append(f"\n**error**\n```\n{err}\n```\n")
    out.append(f"\n**assistant**\n```\n{assistant}\n```\n")
    with path.open("a", encoding="utf-8") as f:
        f.write("".join(out))


def print_system(system: str) -> None:
    bar = "─" * 60
    print(f"\n{bar}\n# SYSTEM PROMPT\n{bar}\n{system}\n{bar}\n", flush=True)


def print_conversation(messages: list[dict]) -> None:
    bar = "─" * 60
    print(f"\n{bar}\n# CONVERSATION (will be sent on next turn)\n{bar}")
    for m in messages:
        print(f"\n[{m['role'].upper()}]\n{m['content']}")
    print(bar, flush=True)


def stream_completion(model: str, messages: list[dict]) -> tuple[str, dict]:
    """Run one streaming completion. Returns (full_text, usage_dict)."""
    from litellm import completion  # imported here so --help is fast

    t0 = time.time()
    full = []
    usage = {}
    try:
        stream = completion(model=model, messages=messages, stream=True)
        for chunk in stream:
            delta = chunk.choices[0].delta.content if chunk.choices else None
            if delta:
                print(delta, end="", flush=True)
                full.append(delta)
            # Some providers attach usage on the final chunk
            chunk_usage = getattr(chunk, "usage", None)
            if chunk_usage:
                usage = {
                    "prompt_tokens": getattr(chunk_usage, "prompt_tokens", None),
                    "completion_tokens": getattr(chunk_usage, "completion_tokens", None),
                    "total_tokens": getattr(chunk_usage, "total_tokens", None),
                    "cost": getattr(chunk_usage, "cost", None),
                }
    except Exception as e:
        print(f"\n[error] {type(e).__name__}: {e}", flush=True)
        return "", {"error": str(e), "elapsed_ms": int((time.time() - t0) * 1000)}

    print()
    return "".join(full), {**usage, "elapsed_ms": int((time.time() - t0) * 1000)}


def save_conversation(path: Path, model: str, system: str, messages: list[dict]) -> None:
    out = [f"# Conversation\n", f"_model_: `{model}`\n", f"_saved_: {now_iso()}\n"]
    out.append("\n## System prompt\n")
    out.append(system + "\n")
    out.append("\n## Messages\n")
    for m in messages:
        out.append(f"\n### {m['role']}\n{m['content']}\n")
    path.write_text("".join(out), encoding="utf-8")
    print(f"[saved] {path}")


HELP = """\
slash commands:
  /show          full conversation (what's sent next turn)
  /system        the system prompt
  /reset         clear history; keep system + variables
  /model NAME    switch model mid-conversation
  /save PATH     dump conversation to a markdown file
  /help          this list
  /quit          exit
"""


def main() -> int:
    load_dotenv()

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default="anthropic/claude-3-5-sonnet-20241022",
                    help="any LiteLLM model string (default: %(default)s)")
    ap.add_argument("--prompt", type=Path, required=True,
                    help="path to system-prompt file (markdown / text)")
    ap.add_argument("--var", action="append", default=[],
                    help="KEY=VALUE; substitute {{KEY}} in the prompt (repeatable)")
    ap.add_argument("--log", type=Path, default=Path("chat.log.jsonl"),
                    help="JSONL turn log (default: %(default)s)")
    ap.add_argument("--transcript", type=Path, default=Path("chat.transcript.md"),
                    help="Human-readable markdown transcript (default: %(default)s)")
    ap.add_argument("--quiet", action="store_true",
                    help="don't print the system prompt at startup")
    args = ap.parse_args()

    if os.getenv("LITELLM_QUIET", "1") == "1":
        os.environ.setdefault("LITELLM_LOG", "ERROR")

    vars_ = parse_vars(args.var)
    system = load_prompt(args.prompt, vars_)

    messages: list[dict] = []   # user/assistant turns; system held separately
    model = args.model

    print(f"\n[chat] model: {model}")
    print(f"[chat] prompt: {args.prompt}  (vars: {len(vars_)})")
    print(f"[chat] log:        {args.log}        (jsonl, machine-readable)")
    print(f"[chat] transcript: {args.transcript}  (markdown, human-readable)")
    print(f"[chat] type /help for commands.  Ctrl-D to quit.\n")

    write_transcript_header(args.transcript, model, system, vars_)

    if not args.quiet:
        print_system(system)

    while True:
        try:
            user = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        if not user:
            continue

        if user.startswith("/"):
            cmd, *rest = user.split(maxsplit=1)
            arg = rest[0] if rest else ""
            if cmd == "/quit":
                return 0
            if cmd == "/help":
                print(HELP)
                continue
            if cmd == "/system":
                print_system(system)
                continue
            if cmd == "/show":
                print_conversation([{"role": "system", "content": system}, *messages])
                continue
            if cmd == "/reset":
                messages = []
                print("[reset] conversation cleared")
                continue
            if cmd == "/model":
                if not arg:
                    print(f"[model] current: {model}")
                else:
                    model = arg.strip()
                    print(f"[model] switched to: {model}")
                continue
            if cmd == "/save":
                if not arg:
                    print("[save] need a path: /save PATH")
                    continue
                save_conversation(Path(arg), model, system, messages)
                continue
            print(f"[unknown] {cmd}.  /help for list.")
            continue

        messages.append({"role": "user", "content": user})

        full_messages = [{"role": "system", "content": system}, *messages]

        print(f"\n[turn] sending {len(full_messages)} messages to {model}")
        print("assistant> ", end="", flush=True)

        ts = now_iso()
        text, meta = stream_completion(model, full_messages)

        if text:
            messages.append({"role": "assistant", "content": text})

        append_log(args.log, {
            "ts": ts,
            "model": model,
            "system_chars": len(system),
            "messages_sent": full_messages,
            "user": user,
            "assistant": text,
            **meta,
        })
        append_transcript_turn(args.transcript, ts, model, user, text, meta)

        print()


if __name__ == "__main__":
    sys.exit(main())
