"""LLM package.

- ``complete``  — one-shot non-streaming chat completion (used by adapters).
- ``cli``       — interactive streaming CLI (``python -m llm.cli``).
"""

from llm._client import CompletionMeta, complete

__all__ = ["CompletionMeta", "complete"]
