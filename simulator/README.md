# Simulator

Top-level operator console. Edit master data (customer / dealer / vehicle /
slots), pick a service type + summary, press Fire. The simulator hands the
synthesized trigger to the case manager (in `../11Labs`) which places the
ElevenLabs call. Case events stream back over `/ws/log`.

## Layout

```
simulator/
├── src/simulator/      # the package
├── tests/
├── pyproject.toml
├── start-simulator.command
└── README.md
```

The simulator imports domain code (`guidepoint.case`, `guidepoint.master_data`,
etc.) from the sibling `../11Labs/` project, installed as an editable package.

## First-time setup

```bash
cd simulator
python3 -m venv .venv
source .venv/bin/activate
pip install -e ../11Labs   # the guidepoint domain package
pip install -e .           # this package (simulator)
```

## Run

Either double-click `start-simulator.command` in Finder, or:

```bash
cd simulator
source .venv/bin/activate
python -m simulator --project-root ../11Labs
```

`--project-root` points at the directory holding `config/`, `fixtures/`, and
`.env` — currently the `11Labs` folder.

Then open http://127.0.0.1:8000.

## Tests

```bash
cd simulator
source .venv/bin/activate
pip install -e ".[dev]"
pytest
```
