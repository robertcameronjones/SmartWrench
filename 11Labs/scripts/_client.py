"""Shared ElevenLabs client builder.

Loads ``ELEVENLABS_API_KEY`` from the environment (or a local ``.env``)
and returns a configured :class:`elevenlabs.client.ElevenLabs` instance.
"""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv
from elevenlabs.client import ElevenLabs


def get_client() -> ElevenLabs:
    load_dotenv()
    api_key = os.getenv("ELEVENLABS_API_KEY")
    if not api_key:
        sys.exit("ELEVENLABS_API_KEY is not set. Copy .env.example to .env and add your key.")
    return ElevenLabs(api_key=api_key)
