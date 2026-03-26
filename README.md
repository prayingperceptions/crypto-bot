# Kalshi Crypto Market Maker 🤖📈

An autonomous, low-latency cryptocurrency prediction market maker built for [Kalshi](https://kalshi.com). 

The bot dynamically discovers live crypto markets, computes fair values using Black-Scholes pricing with live implied volatility from Deribit, and posts optimized bid/ask limit orders to farm the spread — 24/7, fully hands-off.

## How It Works

1. **Market Discovery** — Scans Kalshi's `KXBTCD` hourly events via the Events API, fetches all tail markets ("BTC above $X"), and selects the one with fair value closest to 50¢ and sufficient open interest
2. **Fair Value Engine** — Feeds sub-100ms Binance spot prices through Black-Scholes with live DVOL (implied volatility from Deribit) and real time-to-expiry
3. **Market Making** — Posts BID (fair value - 2¢) and ASK (fair value + 2¢) limit orders. Profits from the 4¢ spread when both sides fill
4. **Auto-Rotation** — Rescans every 15 min, seamlessly switches to the next hourly event before the current one expires (including WS resubscription)
5. **Dynamic Sizing** — Fetches live portfolio balance from Kalshi, sizes orders at 10% of total balance per side. As profits compound, order sizes scale automatically

## Architecture

```
┌─────────────┐     ┌──────────────┐     ┌──────────────────┐
│ Binance WS   │────▶│  HFT Engine  │────▶│  Kalshi REST/WS  │
│ (BTCUSDT L1) │     │ Black-Scholes│     │  (Orders + L2)   │
└─────────────┘     └──────┬───────┘     └──────────────────┘
                           │
              ┌────────────┼────────────┐
              ▼            ▼            ▼
        ┌──────────┐ ┌──────────┐ ┌──────────┐
        │ Deribit   │ │ Scanner  │ │ Telegram │
        │ (DVOL/IV) │ │ (Events) │ │ (Alerts) │
        └──────────┘ └──────────┘ └──────────┘
```

### Core Files
| File | Purpose |
|------|---------|
| `hft_scalper.py` | Main engine — Binance WS, fair value calc, order posting, market rotation |
| `core/market_scanner.py` | Dynamic market discovery via Kalshi Events API (multi-crypto ready) |
| `core/kalshi_client.py` | Async REST + WebSocket client with RSA-PSS auth and auto-reconnect |
| `core/black_scholes.py` | Fair value probability calculator (above-strike + in-range) |
| `core/deribit.py` | Live BTC spot price and DVOL (implied volatility) from Deribit |
| `core/telegram.py` | Heartbeat, market switch, and PnL notifications |
| `core/db.py` | SQLite trade ledger for position tracking across restarts |

## Risk Controls
- **Dynamic Capital Limit** — Uses full Kalshi portfolio balance, refreshed every 15 min
- **10% Per-Trade Sizing** — Each order deploys max 10% of balance per side
- **Hard Stop Loss** — If fair value drops below 15¢ while holding YES, market-sells immediately
- **Auto-Reconnect** — Both Kalshi and Binance WebSockets reconnect on disconnect (5s backoff)
- **Zero Exposed Keys** — All secrets via environment variables, gitignored

## Multi-Crypto Ready
The `CRYPTO_SERIES` dict in `market_scanner.py` is ready for expansion:
```python
CRYPTO_SERIES = {
    "KXBTCD": {"name": "BTC", "binance_symbol": "BTCUSDT"},
    # "KXETHD": {"name": "ETH", "binance_symbol": "ETHUSDT"},
    # "KXSOLD": {"name": "SOL", "binance_symbol": "SOLUSDT"},
}
```

## Deployment

### Requirements
- Kalshi account with API access (Production)
- Kalshi RSA private key
- Telegram bot token + chat ID

### Run on Railway 🚂
1. Link this GitHub repo to a new Railway project
2. Railway auto-detects the `Dockerfile` and `Procfile`
3. Add environment variables:
   - `KALSHI_API_KEY`
   - `KALSHI_PRIVATE_KEY` (raw PEM content)
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
4. Deploy — you'll receive a Telegram heartbeat within 60 seconds

### Run Locally (Docker)
```bash
docker-compose up -d --build
```

## Disclaimer
This is automated trading software. Prediction markets and cryptocurrency are highly volatile. Never run this with more capital than you are willing to lose.

All glory to Jesus
