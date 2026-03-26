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
TRADE_FRACTION = 0.10  # Max 10% of balance per trade
MARKET_RESCAN_INTERVAL = 900  # Re-scan every 15 min (events are hourly)

class HftEngine:
    def __init__(self):
        self.kalshi = KalshiClient(is_demo=False)
        self.scanner = MarketScanner(kalshi_client=self.kalshi)
        self.l2_store = OrderBookStore()
        self.binance_ws: BinanceWSClient | None = None
        self.last_fair_value = 0
        
        # Dynamic market state (populated on boot via discover_and_set_market)
        self.target_market: str = ""
        self.target_strike: float = 0.0
        self.expiry_dt: datetime | None = None
        self.iv: float = 50.0  # Will be replaced by live DVOL
        
        # Risk State — balance fetched dynamically from Kalshi
        self.max_capital: float = 0.0  # Set from portfolio balance on boot
        self.deployed_capital = 0.0
        self.active_positions = 0
        
    def _init_binance_ws(self, symbol: str):
        """Initialize or reinitialize Binance WS for a given crypto symbol."""
        self.binance_ws = BinanceWSClient(symbol=symbol)
        self.binance_ws.on_price_update = self.on_binance_price
        
    def on_binance_price(self, mid: float, bid: float, ask: float):
        """Called on every Binance L1 update (sub-100ms)."""
        if not self.target_market:
            return  # No market selected yet
            
        k_bid, k_bid_qty, k_ask, k_ask_qty = self.l2_store.get_top_of_book(self.target_market)
        
        self.evaluate_trade_trigger(mid, k_bid, k_bid_qty, k_ask, k_ask_qty)
        self.evaluate_exit_trigger(mid, k_bid, k_bid_qty)
        
    def evaluate_exit_trigger(self, binance_price: float, k_bid: int, k_bid_qty: int):
        if self.active_positions <= 0:
            return
            
        fair_value_cents = self._compute_fair_value_cents(binance_price)
        if fair_value_cents is None:
            return
        
        # STOP LOSS: If fair value drops and we hold YES inventory, dump before it hits 0.
        if fair_value_cents < 15 or k_bid < 10:
            logger.warning(f"🚨 [STOP LOSS] Derisking {self.active_positions} contracts! FV: {fair_value_cents}c")
            
            qty_to_sell = min(self.active_positions, k_bid_qty)
            if qty_to_sell > 0:
                client_id = f"sl_{int(time.time())}"
                asyncio.create_task(self.kalshi.place_order(self.target_market, "sell", qty_to_sell, k_bid, client_id, order_type="market"))
                
                self.active_positions -= qty_to_sell
                self.deployed_capital -= (qty_to_sell * 0.10) 
                time.sleep(1)
                
    async def reconcile_positions(self):
        """Fetch currently open tracking positions on boot."""
        logger.info("Reconciling active positions against Kalshi...")
        resp = await self.kalshi.get_positions()
        if "positions" in resp:
            for position in resp["positions"]:
                if position.get("ticker") == self.target_market:
                    self.active_positions = position.get("position", 0)
        logger.info(f"Reconciliation Complete. Active {self.target_market} positions: {self.active_positions}")
        
    def _get_days_to_expiry(self) -> float:
        """Calculate real time-to-expiry from market close_time."""
        if not self.expiry_dt:
            return 0.0
        delta = self.expiry_dt - datetime.now(timezone.utc)
        return max(delta.total_seconds() / 86400.0, 0.0)
        
    def _compute_fair_value_cents(self, spot: float) -> int | None:
        """Calculate fair value in cents using Black-Scholes with live params."""
        days = self._get_days_to_expiry()
        if days <= 0 or self.target_strike <= 0:
            return None
        prob = calculate_probability_above_strike(spot, self.target_strike, days, self.iv)
        return int(prob * 100)

    async def fetch_balance(self):
        """Fetch live portfolio balance from Kalshi and use as capital limit."""
        try:
            resp = await self.kalshi.get_balance()
            # Kalshi returns balance in cents or dollars depending on endpoint
            balance = float(resp.get("balance", 0))
            # If balance looks like cents (> 100), convert to dollars
            if balance > 100:
                balance = balance / 100.0
            if balance > 0:
                self.max_capital = balance
                logger.info(f"💰 Portfolio balance: ${self.max_capital:.2f}")
            else:
                logger.warning(f"Could not fetch balance, using current: ${self.max_capital:.2f}")
        except Exception as e:
            logger.error(f"Balance fetch failed: {e}")

    def evaluate_trade_trigger(self, binance_price: float, k_bid: int, k_bid_qty: int, k_ask: int, k_ask_qty: int):
        # 1. Fair Value from Black-Scholes with live IV & real expiry
        fair_value_cents = self._compute_fair_value_cents(binance_price)
        if fair_value_cents is None:
            return  # Contract expired or invalid state
        
        # 2. Define Market Maker Spread (2 cents wide on each side)
        my_bid = max(1, fair_value_cents - 2)
        my_ask = min(99, fair_value_cents + 2)
        
        # 3. Only re-post limit orders if fair value shifts by at least 1 cent
        if abs(fair_value_cents - self.last_fair_value) >= 1:
            hours_left = self._get_days_to_expiry() * 24
            logger.info(
                f"🔄 [MARKET MAKER] Spot: {binance_price:.2f} | FV: {fair_value_cents}c | "
                f"IV: {self.iv:.0f}% | Exp: {hours_left:.1f}h -> BID: {my_bid}c | ASK: {my_ask}c | "
                f"{self.target_market}"
            )
            self.last_fair_value = fair_value_cents
            
            # Deploy 10% of portfolio balance into each side
            target_trade_usd = self.max_capital * TRADE_FRACTION
            if target_trade_usd <= 0:
                return  # No capital available
            max_contracts_bid = int((target_trade_usd * 100) / my_bid) if my_bid > 0 else 0
            max_contracts_ask = int((target_trade_usd * 100) / my_ask) if my_ask > 0 else 0
            
            client_id_buy = f"mm_buy_{int(time.time())}"
            client_id_sell = f"mm_sell_{int(time.time())}"
            
            asyncio.create_task(self.kalshi.place_order(self.target_market, "buy", max_contracts_bid, my_bid, client_id_buy, order_type="limit"))
            asyncio.create_task(self.kalshi.place_order(self.target_market, "sell", max_contracts_ask, my_ask, client_id_sell, order_type="limit"))

    async def discover_and_set_market(self):
        """Fetch current spot, discover best Kalshi market, and set engine state."""
        from core.telegram import send_telegram_market_switch
        
        spot = await get_btc_price()
        if spot <= 0:
            logger.error("Failed to fetch BTC spot price. Cannot discover markets.")
            return False
        
        self.iv = await get_btc_dvol()
        
        best = await self.scanner.select_best_market(spot, iv=self.iv)
        if not best:
            logger.error("No suitable markets found. Bot cannot trade.")
            return False
        
        old_market = self.target_market
        self.target_market = best["ticker"]
        self.target_strike = best["strike"]
        self.expiry_dt = best.get("expiry_dt")
        
        # Initialize or update Binance WS for the correct crypto
        binance_symbol = best.get("binance_symbol", "BTCUSDT")
        if not self.binance_ws or self.binance_ws.symbol != binance_symbol.lower():
            self._init_binance_ws(binance_symbol)
        
        fair_val = best.get("fair_value_cents", 0)
        self.last_fair_value = fair_val  # Cache for heartbeat before first Binance tick
        oi = best.get("open_interest", 0)
        logger.info(
            f"✅ Engine: {self.target_market} | above ${self.target_strike:,.0f} | "
            f"FV: {fair_val}c | IV: {self.iv:.0f}% | OI: {oi:,.0f} | "
            f"Exp: {best['days_to_expiry']*24:.1f}h"
        )
        
        if old_market != self.target_market:
            # Resubscribe Kalshi WS to new market's L2 book
            await self.kalshi.switch_market(old_market, self.target_market)
            await send_telegram_market_switch(self.target_market, self.target_strike, best["days_to_expiry"], fair_val)
        
        return True

    async def market_rescan_loop(self):
        """Periodically re-scan for a better market."""
        while True:
            await asyncio.sleep(MARKET_RESCAN_INTERVAL)
            logger.info("🔎 Hourly market re-scan triggered...")
            try:
                await self.discover_and_set_market()
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
            await send_telegram_heartbeat(self.target_market, self.last_fair_value)
            await send_telegram_pnl(self.deployed_capital, 0.0) 
            await asyncio.sleep(60 * 60 * 6)

    async def run(self):
        logger.info("Starting HFT Engine with dynamic market discovery...")
        
        # 1. Fetch portfolio balance
        await self.fetch_balance()
        if self.max_capital <= 0:
            logger.error("FATAL: Portfolio balance is $0. Cannot trade.")
            return
        
        # 2. Discover best market on boot
        success = await self.discover_and_set_market()
        if not success:
            logger.error("FATAL: Could not discover any active market. Exiting.")
            return
        
        # 3. Reconcile existing positions  
        await self.reconcile_positions()
        
        # 4. Run all event loops concurrently
        logger.info(f"Launching all event loops | Market: {self.target_market} | Capital: ${self.max_capital:.2f}")
        await asyncio.gather(
            self.binance_ws.connect(),
            self.kalshi.connect_ws([self.target_market], self.l2_store),
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
