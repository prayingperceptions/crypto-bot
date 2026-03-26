# Kalshi Crypto Arb Bot 🤖📈

An asynchronous, low-latency cryptocurrency prediction market trading bot specifically built for [Kalshi](https://kalshi.com). 

It rapidly fetches sub-100ms Spot L1 data from Binance.US, feeds it through a customized Black-Scholes probability model to determine the exact 'Fair Value' of a Kalshi contract, and autonomously posts optimized BID and ASK limit orders on Kalshi to farm the spread.

## Core Architecture
- **`hft_scalper.py`**: The High-Frequency Trading engine. Runs the primary `asyncio` loop handling Binance websockets, Kalshi WebSockets, L2 Orderbook reconstruction, and firing limit orders dynamically.
- **`core/kalshi_client.py`**: Robust async REST/WebSocket wrapper for the Kalshi API, fully integrated with RSA-PSS private key signatures and exponential backoff auto-healing.
- **`core/db.py`**: Local SQLite Trade Ledger. Safely tracks your active positions so nothing is lost during restarts.
- **`core/telegram.py`**: Live reporting module. Pushes out 6-hour system health heartbeats and trade updates straight to your phone.

## Security & Risk Parameters
- **$50 Max Exposure**: The bot has a hard-coded global circuit breaker preventing it from ever putting more than $50 at risk simultaneously.
- **Hard Stop Loss**: If Kalshi's fair value bleeds rapidly beyond our baseline thresholds, the bot abandons limit orders and market-sells its inventory into the bids to evacuate the position instantly.
- **Zero Exposed Keys**: All keys operate entirely out of a local `.env` and `kalshi.key` config hidden from version control.

## Deployment Instructions

### 1. Requirements
- A Kalshi account with API access enabled (Production).
- A `.env` file populated with your Telegram keys.
- A `kalshi.key` file housing your raw Kalshi RSA private key.

### 2. Run Locally (Docker)
Ensure Docker is installed on your machine, then run:
```bash
docker-compose up -d --build
```

### 3. Run on Railway 🚂
1. Link this GitHub repo directly to a new Railway project.
2. Railway will instantly detect the included `Dockerfile` and `Procfile`.
3. Go to the project **Variables** tab and inject:
   - `KALSHI_API_KEY`
   - `KALSHI_PRIVATE_KEY`
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
4. The container will automatically spin up and you'll receive a Telegram heartbeat!

## Disclaimer
This is automated trading software. Prediction markets and cryptocurrency are highly volatile. Never run this with more capital than you are willing to lose.

All glory to Jesus 
