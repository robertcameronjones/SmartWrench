"""Speech-to-Text: transcribe an audio file with ElevenLabs Scribe.

Usage:
    python scripts/stt.py path/to/audio.mp3
    python scripts/stt.py recording.wav --language en --diarize
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from _client import get_client


def main() -> None:
    parser = argparse.ArgumentParser(description="ElevenLabs speech-to-text")
    parser.add_argument("audio", help="Path to an audio file (mp3, wav, m4a, etc.)")
    parser.add_argument(
        "--model",
        default="scribe_v1",
        help="STT model id (default: scribe_v1)",
    )
    parser.add_argument(
        "--language",
        default=None,
        help="ISO-639 language code; omit to auto-detect",
    )
    parser.add_argument(
        "--diarize",
        action="store_true",
        help="Tag speakers in the output",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the full response as JSON instead of just the transcript",
    )
    args = parser.parse_args()

    audio_path = Path(args.audio)
    if not audio_path.is_file():
        sys.exit(f"Audio file not found: {audio_path}")

    client = get_client()
    with audio_path.open("rb") as f:
        result = client.speech_to_text.convert(
            file=f,
            model_id=args.model,
            language_code=args.language,
            diarize=args.diarize,
        )

    if args.json:
        payload = result.model_dump() if hasattr(result, "model_dump") else result
        print(json.dumps(payload, indent=2, default=str))
    else:
        text = getattr(result, "text", None) or str(result)
        print(text)


if __name__ == "__main__":
    main()
