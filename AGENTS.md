# AGENTS.md

## Cursor Cloud specific instructions

### Overview

This is a Python-based algorithmic trading system for QQQ (US ETF). It uses a two-tier signal pipeline:

1. **Python Signal Generators** (`simulate_*.py`) — connect to the Longport OpenAPI for market data, compute trading signals (BUY/SELL/CLOSE), and write them to per-broker SQLite databases.
2. **MetaTrader 5 Expert Advisors** (`SQLiteSignalEA_*.mq5`) — read signals from SQLite and execute trades (Windows-only, cannot run on Linux).

### Dependencies

Install with: `pip install -r requirements.txt`

Required packages: `pandas`, `numpy`, `pytz`, `python-dotenv`, `longport`

### Linting

```
flake8 --max-line-length=200 *.py
```

The codebase has existing style issues (whitespace, unused imports). Use `--max-line-length=200` since the code uses long lines.

### Running

- **Standalone utility scripts** (`check_db.py`, `test_three_db_signals.py`, `clear_db_data.py`, `delete_db.py`) work without any API credentials.
- **Trading simulators** (`simulate_ftmo.py`, `simulate_fundednext.py`, `simulate_the5ers.py`, `simulate_icmarkets.py`, `simulate_ttp.py`, `simulate_blueberry.py`) require Longport API credentials via a `.env` file or environment variables:
  - `LONGPORT_APP_KEY`
  - `LONGPORT_APP_SECRET`
  - `LONGPORT_ACCESS_TOKEN`
- **Data fetcher** (`data_fetch_from_longport.py`) also requires Longport credentials.

### Key gotchas

- The `.mq5` files are MetaTrader 5 Expert Advisors and **cannot be compiled or run on Linux**. They require a Windows machine with MT5 installed.
- The main simulation scripts (`simulate_*.py`) connect to the Longport API at **module import time** (line 198 in `simulate_ftmo.py`: `QUOTE_CTX, TRADE_CTX = create_contexts()`). This means you cannot import or run them without valid API credentials.
- **Longport access tokens expire**. If you get `401003 token expired`, the token must be regenerated from the [Longport OpenAPI console](https://open.longportapp.com/). The SDK's `config.refresh_access_token()` will also fail if the token is fully expired. Update `LONGPORT_ACCESS_TOKEN` after regeneration.
- SQLite database files (`*.db`) are gitignored.
- All comments and log messages are in Chinese (Simplified Chinese).
- On Linux, SQLite DBs are created in the current working directory. On Windows, they go to the MT5 common files directory.
- The MQ5 EAs rely on `OnTick()` to poll the database. If a broker (e.g. IC Markets) delivers ticks late at market open, signals may be delayed. A potential fix is adding `OnTimer()` as a fallback.
- IC Markets EA has a known issue: it frequently stops and restarts (visible in MT5 logs as repeated "EA已停止" → reinitialization cycles). Root cause is under investigation.

### Quick verification

```bash
python3 -c "
from longport.openapi import Config, QuoteContext
ctx = QuoteContext(Config.from_env())
q = ctx.quote(['QQQ.US'])[0]
print(f'QQQ.US = \${q.last_done}')
"
```
