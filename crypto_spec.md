# Kalshi Crypto Arb Bot — Architecture & Scope

This document serves as the foundation for the new `crypto-bot` project. Feed this to your core agent to immediately sync context.

## 🎯 Objective
Build an automated trading system for Kalshi's cryptocurrency prediction markets. The bot will operate in two distinct modes to capture entirely different market inefficiencies:

1. **The Scalper (1m/5m Markets):** High-frequency execution using raw WebSockets.
2. **The Swing Tracer (Daily/Weekly Markets):** Low-frequency polling using momentum/options data.

---

## 🏗️ Architecture Split
Because 5-minute markets require a persistent connection and 0-latency execution, mixing them with slow daily polls is an architectural anti-pattern. The bot will use two separate entry points running completely isolated event loops.

### Component 1: `hft_scalper.py` (1m & 5m Markets)
- **Engine:** Python `asyncio` + `websockets`
- **Data Source:** Binance or Coinbase WebSockets (Spot + Futures).
- **Latency Requirement:** < 100ms.
- **Kalshi API:** Uses the Kalshi WebSocket feed (`wss://api.elections.kalshi.com/trade-api/ws/v2`) to monitor the exact order book (L2) rather than just the generic "ask" price. 
- **The Edge:** If Bitcoin suddenly spikes on Binance, Kalshi's limit order book takes seconds for human market makers to adjust. The bot detects the spread between live Binance Spot and the Kalshi 5m threshold, and fires an order instantly to capture the mispricing.
- **Risk Layer:** Order book depth check. Never place an order larger than the available liquidity on Kalshi's top bid/ask (to prevent slipping down the book and ruining the edge).

### Component 2: `swing_tracer.py` (Daily & Weekly Markets)
- **Engine:** Standard `requests` + `schedule` (similar to Kalshi Weather Bot).
- **Data Source:** Deribit Implied Volatility (DVOL) or Binance Options data.
- **Latency Requirement:** Not sensitive (runs every hour).
- **Kalshi API:** Standard REST `GET /markets`.
- **The Edge:** Kalshi's weekly markets (e.g., "Will BTC touch $100k by Friday?") are priced by retail sentiment. We will price them using the Black-Scholes model based on actual live options implied volatility from Deribit. If Kalshi is severely under-pricing a move relative to institutional options markets, the bot buys it.

---

## 🛠️ Tech Stack & Requirements

- `python 3.10+`
- `asyncio` + `websockets` (for HFT scalper)
- `ccxt` or `binance-connector` (for live crypto pricing)
- `scipy` + `numpy` (for Black-Scholes daily/weekly calculations)
- `python-dotenv` (for API keys)
- `.env` config with:
  - `KALSHI_API_KEY`
  - `KALSHI_PRIVATE_KEY`
  - `TELEGRAM_BOT_TOKEN`
  - `TELEGRAM_CHAT_ID`

---

## 🚀 Execution Plan (Phased)

### Phase 1: Core Infrastructure
- Scaffolding (dotenv, logger, Telegram alerts).
- Build the Kalshi async client (RSA-PSS authentication wrapper adapted for `aiohttp` and WebSockets).

### Phase 2: The Scalper (HFT)
- Connect to Binance WebSocket.
- Connect to Kalshi WebSocket.
- Build L2 order book synchronizer to keep track of Kalshi's liquidity locally.
- Implement the millisecond delta trigger (Spot Price vs Kalshi Strike threshold).
- Test on Kalshi Demo Environment (`demo-api.kalshi.co`).

### Phase 3: The Swing Tracer (Daily/Weekly)
- Integrate Deribit REST API for live Implied Volatility.
- Implement Black-Scholes probability calculator.
- Wire generic REST cron-job for daily executions.

---

*Agent Instruction:* Use this document to generate `task.md` and begin scaffolding Phase 1.
