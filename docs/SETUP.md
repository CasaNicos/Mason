# Setting up Mason

Mason is a Flask app (`mason_app.py`) that serves a web dashboard on port 5050. It is developed on Ubuntu
with Python 3.12+.

## 1. Prerequisites

- Python 3.12 or newer.
- API keys: Anthropic and an Alpaca **paper** account at minimum. See [SECRETS.md](SECRETS.md).
- Optional: [Ollama](https://ollama.com) for the local models (`qwen3.5:4b`, `phi4-mini`) that handle cheap
  background work. Without it, those paths are skipped or fall back to Claude.
- Optional: Node 20+ only if you want to rebuild the React widget bundle in `ui/`. The built bundle is
  already in `static/ui/`.

## 2. Install

```bash
git clone https://github.com/CasaNicos/MASON.git mason
cd mason
pip install -r requirements.txt
```

`requirements.txt` is a full environment snapshot. The core needs are Flask, anthropic, requests, yfinance,
pandas, numpy and the Alpaca/HTTP clients; install more as imports ask for them.

## 3. Configure

```bash
cp mason.config.example.json mason.config.json
mkdir -p ~/.config/mason && chmod 700 ~/.config/mason
cp mason.secrets.example.json ~/.config/mason/mason.secrets.json
chmod 600 ~/.config/mason/mason.secrets.json
```

Fill in the keys in the secrets file. In `mason.config.json`, every feature flag (`enabled`) ships as `false`;
turn on the parts you want. Set `mason.owner` to your name. Robinhood features need
`python3 mason_robinhood.py login` first.

`mason.config.json` and everything Mason writes at runtime (SQLite databases, journals, logs) are listed in
`.gitignore`.

## 4. Run

```bash
python3 mason_app.py        # or ./launch_mason.sh
```

Open http://127.0.0.1:5050. The v2 dashboard is at `/v2`.

To run it as a service, point a systemd unit's `ExecStart` at `/usr/bin/python3 /path/to/mason_app.py` with
`WorkingDirectory` set to the repo.
