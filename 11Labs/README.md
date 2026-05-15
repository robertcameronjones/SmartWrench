# ElevenLabs API Scripts

A small Python toolkit for working with the [ElevenLabs](https://elevenlabs.io/) API
using the official `elevenlabs` SDK.

Includes three scripts:

| Script | What it does |
| --- | --- |
| `scripts/tts.py` | Text-to-Speech — write text to an MP3 file |
| `scripts/stt.py` | Speech-to-Text — transcribe an audio file with Scribe |
| `scripts/agent.py` | Manage Conversational AI agents (list / get / create / delete) |

> Note: ElevenLabs Conversational AI agents handle **both voice and SMS** on the
> same agent — channels are configured in the dashboard or via phone-number
> bindings, so there is no separate "SMS API" to call.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# then edit .env and set ELEVENLABS_API_KEY
```

Get an API key at <https://elevenlabs.io/app/settings/api-keys>.

## Usage

### Text-to-Speech

```bash
python scripts/tts.py "The first move is what sets everything in motion."
python scripts/tts.py --file input.txt --voice JBFqnCBsd6RMkjVDRZzb --out out/hello.mp3
```

Outputs default to `out/tts.mp3`.

### Speech-to-Text

```bash
python scripts/stt.py path/to/recording.mp3
python scripts/stt.py recording.wav --language en --diarize --json
```

### Conversational Agents

```bash
python scripts/agent.py list
python scripts/agent.py create --name "Support Bot" \
    --prompt "You are a helpful, concise support agent." \
    --first-message "Hi! How can I help today?"
python scripts/agent.py get --id agent_abc123
python scripts/agent.py delete --id agent_abc123
```

After creating an agent, attach a phone number to it in the ElevenLabs
dashboard (Twilio integration) to enable inbound/outbound voice and SMS.

## Project layout

```
.
├── .env.example
├── README.md
├── requirements.txt
└── scripts/
    ├── _client.py   # shared ElevenLabs client (loads .env)
    ├── tts.py
    ├── stt.py
    └── agent.py
```
