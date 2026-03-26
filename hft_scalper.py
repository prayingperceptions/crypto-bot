import asyncio
import time
from datetime import datetime, timezone
from dotenv import load_dotenv
from core.logger import setup_logger
from core.binance_ws import BinanceWSClient
from core.kalshi_client import KalshiClient
from core.kalshi_l2 import OrderBookStore
from core.market_scanner import MarketScanner
from core.black_scholes import calculate_probability_above_strike
from core.deribit import get_btc_dvol, get_btc_price

logger = setup_logger("hft_scalper")

# Production Params 
MAX_SLIPPAGE_CENTS = 2  # Max slippage tolerance
TRADE_FRACTION = 0.10  # Max 10% of balance per trade per market
NUM_MARKETS = 3  # Trade across 3 simultaneous events
MARKET_RESCAN_INTERVAL = 900  # Re-scan every 15 min

class ActiveMarket:
    """Tracks state for one active market the engine is trading on."""
    def __init__(self, ticker: str, strike: float, expiry_dt: datetime | None,
                 fair_value_cents: int = 0, event_ticker: str = ""):
        self.ticker = ticker
        self.strike = strike
        self.expiry_dt = expiry_dt
        self.event_ticker = event_ticker
        self.last_fair_value = fair_value_cents
        self.active_positions = 0
    
    def get_days_to_expiry(self) -> float:
        if not self.expiry_dt:
            return 0.0
        delta = self.expiry_dt - datetime.now(timezone.utc)
        return max(delta.total_seconds() / 86400.0, 0.0)

class HftEngine:
    def __init__(self):
        self.kalshi = KalshiClient(is_demo=False)
        self.scanner = MarketScanner(kalshi_client=self.kalshi)
        self.l2_store = OrderBookStore()
        self.binance_ws: BinanceWSClient | None = None
        
        # Multiple active markets (one per event/expiry)
        self.markets: list[ActiveMarket] = []
        
        # Shared state
        self.iv: float = 50.0
        self.max_capital: float = 0.0
        self.deployed_capital = 0.0
        
    def _init_binance_ws(self, symbol: str):
        """Initialize Binance WS for a given crypto symbol."""
        self.binance_ws = BinanceWSClient(symbol=symbol)
        self.binance_ws.on_price_update = self.on_binance_price
        
    def on_binance_price(self, mid: float, bid: float, ask: float):
        """Called on every Binance L1 update. Evaluates ALL active markets."""
        for market in self.markets:
            if not market.ticker:
                continue
            k_bid, k_bid_qty, k_ask, k_ask_qty = self.l2_store.get_top_of_book(market.ticker)
            self.evaluate_trade_trigger(market, mid, k_bid, k_bid_qty, k_ask, k_ask_qty)
            self.evaluate_exit_trigger(market, mid, k_bid, k_bid_qty)
        
    def evaluate_exit_trigger(self, market: ActiveMarket, binance_price: float, k_bid: int, k_bid_qty: int):
        if market.active_positions <= 0:
            return
            
        fair_value_cents = self._compute_fair_value_cents(market, binance_price)
        if fair_value_cents is None:
            return
        
        if fair_value_cents < 15 or k_bid < 10:
            logger.warning(f"🚨 [STOP LOSS] {market.ticker} | Derisking {market.active_positions} contracts! FV: {fair_value_cents}c")
            
            qty_to_sell = min(market.active_positions, k_bid_qty)
            if qty_to_sell > 0:
                client_id = f"sl_{market.ticker[-8:]}_{int(time.time())}"
                asyncio.create_task(self.kalshi.place_order(market.ticker, "sell", qty_to_sell, k_bid, client_id, order_type="market"))
                market.active_positions -= qty_to_sell
                self.deployed_capital -= (qty_to_sell * 0.10) 
                time.sleep(1)
                
    async def reconcile_positions(self):
        """Fetch currently open positions on boot."""
        logger.info("Reconciling active positions against Kalshi...")
        resp = await self.kalshi.get_positions()
        if "positions" in resp:
            for position in resp["positions"]:
                for market in self.markets:
                    if position.get("ticker") == market.ticker:
                        market.active_positions = position.get("position", 0)
        total = sum(m.active_positions for m in self.markets)
        logger.info(f"Reconciliation Complete. Total active positions across {len(self.markets)} markets: {total}")
        
    def _compute_fair_value_cents(self, market: ActiveMarket, spot: float) -> int | None:
        """Calculate fair value in cents using Black-Scholes."""
        days = market.get_days_to_expiry()
        if days <= 0 or market.strike <= 0:
            return None
        prob = calculate_probability_above_strike(spot, market.strike, days, self.iv)
        return int(prob * 100)

    async def fetch_balance(self):
        """Fetch live portfolio balance from Kalshi."""
        try:
            resp = await self.kalshi.get_balance()
            balance = float(resp.get("balance", 0))
            if balance > 100:
                balance = balance / 100.0
            if balance > 0:
                self.max_capital = balance
                logger.info(f"💰 Portfolio balance: ${self.max_capital:.2f}")
            else:
                logger.warning(f"Could not fetch balance, using current: ${self.max_capital:.2f}")
        except Exception as e:
            logger.error(f"Balance fetch failed: {e}")

    def evaluate_trade_trigger(self, market: ActiveMarket, binance_price: float, 
                                k_bid: int, k_bid_qty: int, k_ask: int, k_ask_qty: int):
        fair_value_cents = self._compute_fair_value_cents(market, binance_price)
        if fair_value_cents is None:
            return
        
        my_bid = max(1, fair_value_cents - 2)
        my_ask = min(99, fair_value_cents + 2)
        
        if abs(fair_value_cents - market.last_fair_value) >= 1:
            hours_left = market.get_days_to_expiry() * 24
            logger.info(
                f"🔄 [{market.ticker}] Spot: {binance_price:.2f} | FV: {fair_value_cents}c | "
                f"IV: {self.iv:.0f}% | Exp: {hours_left:.1f}h -> BID: {my_bid}c | ASK: {my_ask}c"
            )
            market.last_fair_value = fair_value_cents
            
            # Capital per market = total balance / number of active markets
            capital_per_market = self.max_capital / max(len(self.markets), 1)
            target_trade_usd = capital_per_market * TRADE_FRACTION
            if target_trade_usd <= 0:
                return
            
            max_contracts_bid = int((target_trade_usd * 100) / my_bid) if my_bid > 0 else 0
            max_contracts_ask = int((target_trade_usd * 100) / my_ask) if my_ask > 0 else 0
            
            client_id_buy = f"mm_b_{market.ticker[-8:]}_{int(time.time())}"
            client_id_sell = f"mm_s_{market.ticker[-8:]}_{int(time.time())}"
            
            asyncio.create_task(self.kalshi.place_order(market.ticker, "buy", max_contracts_bid, my_bid, client_id_buy, order_type="limit"))
            asyncio.create_task(self.kalshi.place_order(market.ticker, "sell", max_contracts_ask, my_ask, client_id_sell, order_type="limit"))

    async def discover_and_set_markets(self):
        """Discover top N markets and set engine state."""
        from core.telegram import send_telegram_market_switch
        
        spot = await get_btc_price()
        if spot <= 0:
            logger.error("Failed to fetch BTC spot price.")
            return False
        
        self.iv = await get_btc_dvol()
        
        top_markets = await self.scanner.select_top_n_markets(spot, iv=self.iv, n=NUM_MARKETS)
        if not top_markets:
            logger.error("No suitable markets found.")
            return False
        
        old_tickers = set(m.ticker for m in self.markets)
        new_tickers = set(m["ticker"] for m in top_markets)
        
        # Build new market objects
        new_market_objs = []
        for mkt in top_markets:
            new_market_objs.append(ActiveMarket(
                ticker=mkt["ticker"],
                strike=mkt["strike"],
                expiry_dt=mkt.get("expiry_dt"),
                fair_value_cents=mkt.get("fair_value_cents", 0),
                event_ticker=mkt.get("event_ticker", ""),
            ))
        
        self.markets = new_market_objs
        
        # Initialize Binance WS for the correct crypto
        binance_symbol = top_markets[0].get("binance_symbol", "BTCUSDT")
        if not self.binance_ws or self.binance_ws.symbol != binance_symbol.lower():
            self._init_binance_ws(binance_symbol)
        
        # Switch Kalshi WS subscriptions
        tickers_to_unsub = old_tickers - new_tickers
        tickers_to_sub = new_tickers - old_tickers
        
        if tickers_to_unsub and self.kalshi._ws:
            for t in tickers_to_unsub:
                try:
                    await self.kalshi._send_unsubscribe(self.kalshi._ws, [t])
                except Exception:
                    pass
        if tickers_to_sub and self.kalshi._ws:
            for t in tickers_to_sub:
                try:
                    await self.kalshi._send_subscribe(self.kalshi._ws, [t])
                except Exception:
                    pass
        
        # Log and notify
        for mkt in self.markets:
            fv = mkt.last_fair_value
            logger.info(
                f"✅ Active: {mkt.ticker} | above ${mkt.strike:,.0f} | "
                f"FV: {fv}c | Exp: {mkt.get_days_to_expiry()*24:.1f}h"
            )
        
        if new_tickers != old_tickers:
            # Send one consolidated Telegram notification
            market_list = " | ".join(f"{m.ticker} ({m.last_fair_value}c)" for m in self.markets)
            await send_telegram_market_switch(
                market_list, 
                self.markets[0].strike,
                self.markets[0].get_days_to_expiry(),
                self.markets[0].last_fair_value
            )
        
        logger.info(f"💰 Capital: ${self.max_capital:.2f} | ${self.max_capital/max(len(self.markets),1):.2f} per market")
        return True

    async def market_rescan_loop(self):
        """Periodically re-scan for better markets."""
        while True:
            await asyncio.sleep(MARKET_RESCAN_INTERVAL)
            logger.info(f"🔎 Market re-scan triggered (every {MARKET_RESCAN_INTERVAL//60}min)...")
            try:
                await self.discover_and_set_markets()
            except Exception as e:
                logger.error(f"Market re-scan failed: {e}")

    async def iv_and_balance_refresh_loop(self):
        """Refresh DVOL and portfolio balance every 15 minutes."""
        while True:
            await asyncio.sleep(900)
            try:
                new_iv = await get_btc_dvol()
                if new_iv > 0:
                    self.iv = new_iv
                await self.fetch_balance()
            except Exception as e:
                logger.error(f"IV/balance refresh failed: {e}")

    async def heartbeat_loop(self):
        """Send a telegram heartbeat every 6 hours."""
        from core.telegram import send_telegram_heartbeat, send_telegram_pnl
        while True:
            market_summary = ", ".join(f"{m.ticker}({m.last_fair_value}c)" for m in self.markets) or "none"
            await send_telegram_heartbeat(market_summary, len(self.markets))
            await send_telegram_pnl(self.deployed_capital, 0.0) 
            await asyncio.sleep(60 * 60 * 6)

    async def run(self):
        logger.info(f"Starting HFT Engine | Multi-market mode ({NUM_MARKETS} simultaneous)...")
        
        # 1. Fetch portfolio balance
        await self.fetch_balance()
        if self.max_capital <= 0:
            logger.error("FATAL: Portfolio balance is $0. Cannot trade.")
            return
        
        # 2. Discover top N markets
        success = await self.discover_and_set_markets()
        if not success:
            logger.error("FATAL: Could not discover any active markets. Exiting.")
            return
        
        # 3. Reconcile existing positions  
        await self.reconcile_positions()
        
        # 4. Run all event loops concurrently
        all_tickers = [m.ticker for m in self.markets]
        logger.info(f"Launching all event loops | {len(self.markets)} markets | Capital: ${self.max_capital:.2f}")
        await asyncio.gather(
            self.binance_ws.connect(),
            self.kalshi.connect_ws(all_tickers, self.l2_store),
            self.heartbeat_loop(),
            self.market_rescan_loop(),
            self.iv_and_balance_refresh_loop(),
        )

async def main():
    load_dotenv()
    engine = HftEngine()
    
    try:
        await engine.run()
    except KeyboardInterrupt:
        logger.info("Shutting down scalper.")
    finally:
        await engine.kalshi.close()

if __name__ == "__main__":
    asyncio.run(main())
