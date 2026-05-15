"""Text-to-Speech: synthesize text into an MP3 file.

Usage:
    python scripts/tts.py "Hello, world."
    python scripts/tts.py "Hello, world." --voice JBFqnCBsd6RMkjVDRZzb --out out/hello.mp3
    python scripts/tts.py --file input.txt --model eleven_multilingual_v2
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from _client import get_client


def main() -> None:
    parser = argparse.ArgumentParser(description="ElevenLabs text-to-speech")
    parser.add_argument("text", nargs="?", help="Text to synthesize")
    parser.add_argument("--file", help="Read text from a file instead of the CLI arg")
    parser.add_argument(
        "--voice",
        default=os.getenv("ELEVENLABS_VOICE_ID", "JBFqnCBsd6RMkjVDRZzb"),
        help="Voice ID (defaults to ELEVENLABS_VOICE_ID or a sample voice)",
    )
    parser.add_argument(
        "--model",
        default="eleven_multilingual_v2",
        help="Model id, e.g. eleven_v3, eleven_multilingual_v2, eleven_flash_v2_5",
    )
    parser.add_argument(
        "--format",
        default="mp3_44100_128",
        help="Output format, e.g. mp3_44100_128, mp3_44100_192, pcm_16000",
    )
    parser.add_argument(
        "--out",
        default="out/tts.mp3",
        help="Output file path (default: out/tts.mp3)",
    )
    args = parser.parse_args()

    if args.file:
        text = Path(args.file).read_text(encoding="utf-8").strip()
    elif args.text:
        text = args.text
    else:
        sys.exit("Provide text as a positional arg or use --file path/to/text.txt")

    client = get_client()
    audio_iter = client.text_to_speech.convert(
        text=text,
        voice_id=args.voice,
        model_id=args.model,
        output_format=args.format,
    )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("wb") as f:
        for chunk in audio_iter:
            if chunk:
                f.write(chunk)

    print(f"Wrote {out_path} ({out_path.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
