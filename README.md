# Jesse — Autonomous AI Trading Agent

Named after **Jesse Livermore**, the greatest tape reader in history who made $100M+ (≈$1.5B today) purely from price action.

Jesse is a 24/7 autonomous AI trading agent built on LangChain ReAct with persistent memory, circuit breakers, and Telegram control. It consumes [MT5-MCP](https://github.com/ishanp321/MT5-mcp) as its trading backend.

## Architecture

```
┌──────────────┐     ┌─────────────┐     ┌──────────────┐     ┌──────────┐
│  Telegram    │────▶│  Jesse      │────▶│  MT5-MCP     │────▶│  MT5 EA  │
│  Bot         │◀────│  Agent      │◀────│  Server :8010│◀────│  (Wine)  │
│  (commands)  │     │  (ReAct)    │     │  (35 tools)  │     │          │
└──────────────┘     └─────────────┘     └──────────────┘     └──────────┘
                          │
                     ┌────▼─────┐
                     │  Memory  │
                     │ ChromaDB │
                     │  + SQLite│
                     └──────────┘
```

## Quick Start

### Prerequisites

- MT5-MCP services running (ports 8010, 8020, 8025)
- Python 3.11+
- Telegram Bot Token (from @BotFather)

### Installation

```bash
git clone https://github.com/ishanp321/jesse.git
cd jesse
poetry install

export TELEGRAM_BOT_TOKEN="your_bot_token"
export TELEGRAM_CHAT_ID="your_chat_id"
poetry run jesse start
```

### Health Check

```bash
curl http://127.0.0.1:8090/health
```

## Telegram Commands

| Command | Description |
|---|---|
| `/start` | Agent info |
| `/status` | Agent + circuit breaker status |
| `/sleep` | Pause trading cycles |
| `/wake` | Resume trading cycles |
| `/chart [SYMBOLS...]` | Send chart screenshots |
| `/positions` | List open positions with PnL |
| `/pnl` | 7-day performance summary |
| `/scan [SYMBOLS...]` | Quick market scan |
| `/close` | Close all positions |
| `/help` | Command reference |

## Circuit Breakers

- **3 consecutive losses** → 2-hour cool-off
- **Daily loss >5%** → stop trading for 24h
- **Max 10 trades/day** → stop
- **Max 3 open positions** → stop
- **3 bridge failures** → stop

## Configuration

| Variable | Required | Default |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Yes | — |
| `TELEGRAM_CHAT_ID` | Yes | — |
| `MT5_MCP_URL` | No | `http://127.0.0.1:8010` |

## License

MIT
