# Mason secrets

Mason never keeps API keys in the repository. This page lists the key **names**, where Mason looks for
them, and how to get each one. The loader is `mason_secrets.py`.

## Where secrets live

| What | Where |
|---|---|
| Secrets file | `~/.config/mason/mason.secrets.json`. Directory `0700`, file `0600`. Outside the repo, so it can't be committed by accident. |
| Override the path | Set `MASON_SECRETS_FILE=/some/path.json`. |
| Template | `mason.secrets.example.json` at the repo root: every key name with an empty value and a description. |
| Legacy fallback | `mason.config.json` (`apis.*.key`, `paper_trading.api_key` / `api_secret`). Still read when a key is missing from the secrets file. |

**Precedence for each key:** a non-empty environment variable wins, then the secrets file, then the legacy
`mason.config.json`, then the caller's default. The loader never writes `os.environ` and never logs a value.

## Keys

| Name | Used for | Required |
|---|---|---|
| `ANTHROPIC_API_KEY` | Claude models | yes |
| `APCA_API_KEY_ID` / `APCA_API_SECRET_KEY` | Alpaca **paper** trading | yes |
| `FMP_API_KEY` | Financial Modeling Prep: calendars, fundamentals, flows | yes |
| `ALPHA_VANTAGE_API_KEY` | backup quotes | optional |
| `FRED_API_KEY` | macro data | optional |
| `EIA_API_KEY` | energy data | optional |
| `FINVIZ_API_KEY` | FinViz Elite (theme-detector skill) | optional |
| `SEC_USER_AGENT` | SEC EDGAR fair-access User-Agent, e.g. `"Your Name you@example.com"` | optional |
| `ROBINHOOD_TOKEN_FILE` | path of the Robinhood OAuth token file (not a secret value) | optional |

The Robinhood OAuth token itself lives in `~/.config/mason/robinhood_oauth.json` (`0600`) and is created by
`python3 mason_robinhood.py login`.

## How to obtain each key

| Name | Where to get it |
|---|---|
| `ANTHROPIC_API_KEY` | console.anthropic.com → Settings → API Keys. |
| `APCA_API_KEY_ID` / `APCA_API_SECRET_KEY` | app.alpaca.markets → switch to the **Paper** account → API Keys → Generate. |
| `FMP_API_KEY` | site.financialmodelingprep.com → Dashboard → API Keys. |
| `ALPHA_VANTAGE_API_KEY` | www.alphavantage.co/support/#api-key |
| `FRED_API_KEY` | fred.stlouisfed.org → My Account → API Keys. |
| `EIA_API_KEY` | www.eia.gov/opendata/register.php |
| `FINVIZ_API_KEY` | finviz.com Elite → Account → API. |

## Setting up the secrets file

```bash
mkdir -p ~/.config/mason && chmod 700 ~/.config/mason
cp mason.secrets.example.json ~/.config/mason/mason.secrets.json
chmod 600 ~/.config/mason/mason.secrets.json
# then fill in the values
```

If you already have keys in `mason.config.json`, `python3 tools/migrate_secrets.py` (dry run) and
`python3 tools/migrate_secrets.py --confirm` copy them into the secrets file without printing any value.

Never commit the secrets file, paste it into chat, or attach it to an issue.

## Loader API

```python
import mason_secrets as S
S.get("FMP_API_KEY", default="")        # env > file > legacy config > default; never raises
S.anthropic_key(); S.alpaca_keys(); S.fmp_key()
S.status()                             # {name: bool}, presence only
S.missing_required()                   # [names]
S.source("FMP_API_KEY")                # "env" | "file" | "legacy" | "default"
```
