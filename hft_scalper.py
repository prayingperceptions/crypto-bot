import asyncio
import time
from core.binance_ws import BinanceWSClient
from core.binance_rest import BinanceFuturesClient
from core.kalshi_client import KalshiClient
from core.kalshi_l2 import OrderBookStore
from core.black_scholes import calculate_probability_above_strike
from scipy.stats import norm
import numpy as np

logger = setup_logger("hft_scalper")

# Production Params 
TARGET_KALSHI_MARKET = "KXBTCUSD-24M01-100000"  # Ex: $100k Strike
TARGET_STRIKE = 100000.0
DAYS_TO_EXPIRY = 7.0 # Simulated days to expiry for Delta calc

BINANCE_SYMBOL = "BTCUSDT"
MAX_CAPITAL_RISK_USD = 50.0  # Max exposure $50
MAX_SLIPPAGE_CENTS = 2  # Max slippage tolerance
TRADE_FRACTION = 0.10 # Max 10% of base per trade ($5.00)

class HftEngine:
    def __init__(self):
        self.binance_ws = BinanceWSClient(symbol=BINANCE_SYMBOL)
        self.binance_rest = BinanceFuturesClient(testnet=True)
        self.kalshi = KalshiClient(is_demo=True)
        self.l2_store = OrderBookStore()
        
        # Risk State
        self.deployed_capital = 0.0
        self.active_positions = 0
        
        self.binance_ws.on_price_update = self.on_binance_price
        
    def on_binance_price(self, mid: float, bid: float, ask: float):
        # Called on every Binance L1 update (sub-100ms)
        # Compare with Kalshi Top of Book
        k_bid, k_bid_qty, k_ask, k_ask_qty = self.l2_store.get_top_of_book(TARGET_KALSHI_MARKET)
        
        # Only log periodically to avoid I/O bottlenecks
        # logger.debug(f"Binance Mid: {mid:.2f} | Kalshi L2 -> Bid {k_bid}c Ask {k_ask}c")
        
        self.evaluate_trade_trigger(mid, k_bid, k_bid_qty, k_ask, k_ask_qty)
        self.evaluate_exit_trigger(mid, k_bid, k_bid_qty)
        
    def evaluate_exit_trigger(self, binance_price: float, k_bid: int, k_bid_qty: int):
        if self.active_positions <= 0:
            return
            
        # Hard Stop-Loss logic: Never take a 100% loss. 
        # If the fair value drops significantly and we can hit the bid to exit, do it.
        delta = self.calculate_delta(binance_price)
        fair_value_cents = int(delta * 100)
        
        # STOP LOSS: If the fair value drops below say 10 cents, or we lose 20 cents of edge
        if fair_value_cents < 15 or k_bid < 10:
            logger.warning(f"🚨 [STOP LOSS] Derisking {self.active_positions} contracts! Fair Value dropping: {fair_value_cents}c | Bid: {k_bid}c")
            
            qty_to_sell = min(self.active_positions, k_bid_qty)
            if qty_to_sell > 0:
                client_id = f"sl_{int(time.time())}"
                asyncio.create_task(self.kalshi.place_order(TARGET_KALSHI_MARKET, "sell", qty_to_sell, k_bid, client_id))
                
                # Unwind the Binance short hedge (market BUY to close short)
                hedge_relieve_qty = self.calculate_delta(binance_price) * qty_to_sell
                asyncio.create_task(self.binance_rest.place_futures_order(BINANCE_SYMBOL, "BUY", hedge_relieve_qty))
                
                self.active_positions -= qty_to_sell
                # self.deployed_capital is roughly adjusted, though in production you'd track exact fill prices via SQLite
                self.deployed_capital -= (qty_to_sell * 0.10) # rough estimate of recovered capital
                time.sleep(1)
                
    async def reconcile_positions(self):
        """Fetch currently open tracking positions on boot."""
        logger.info("Reconciling active positions against Kalshi...")
        resp = await self.kalshi.get_positions()
        if "positions" in resp:
            for position in resp["positions"]:
                if position.get("ticker") == TARGET_KALSHI_MARKET:
                    self.active_positions = position.get("position", 0)
        logger.info(f"Reconciliation Complete. Active {TARGET_KALSHI_MARKET} positions: {self.active_positions}")
        
    def validate_l2_slippage(self, market: str, side: str, desired_qty: int, max_price: int) -> bool:
        """Ensure order can clear the L2 book without exceeding max_price"""
        # Very simplified mock: verify top of book depth is enough
        top_bid, bid_qty, top_ask, ask_qty = self.l2_store.get_top_of_book(market)
        if side == "buy" and ask_qty >= desired_qty and top_ask <= max_price:
            return True
        if side == "sell" and bid_qty >= desired_qty and top_bid >= max_price:
            return True
        return False
        
    def calculate_delta(self, spot: float) -> float:
        """Calculate the Black-Scholes Nd1 (Delta) for the contract"""
        iv = 0.50 # Simulated 50% IV for rapid execution instead of blocking API
        t_years = DAYS_TO_EXPIRY / 365.0
        d1 = (np.log(spot / TARGET_STRIKE) + (0.05 + 0.5 * iv**2) * t_years) / (iv * np.sqrt(t_years))
        return float(norm.cdf(d1))

    def evaluate_trade_trigger(self, binance_price: float, k_bid: int, k_bid_qty: int, k_ask: int, k_ask_qty: int):
        # 1. Circuit Breaker: Capital bounds
        target_trade_usd = MAX_CAPITAL_RISK_USD * TRADE_FRACTION
        
        # 2. Delta & Fair Value calculation
        delta = self.calculate_delta(binance_price)
        fair_value_cents = int(delta * 100)
        
        # 3. Execution Edge condition
        if k_ask > 0 and (fair_value_cents - k_ask) >= 5: # 5 cent edge
            # How many contracts can we afford with our trade fraction?
            max_contracts = int((target_trade_usd * 100) / k_ask)
            desired_qty = min(max_contracts, k_ask_qty)
            
            # Risk Gate
            if self.deployed_capital + (desired_qty * k_ask / 100.0) > MAX_CAPITAL_RISK_USD:
                logger.debug("Risk Limit Hit. Skipping edge.")
                return
                
            # Slippage Gate
            if not self.validate_l2_slippage(TARGET_KALSHI_MARKET, "buy", desired_qty, k_ask + MAX_SLIPPAGE_CENTS):
                logger.debug("L2 Book too thin for desired qty. Skipping to avoid slippage.")
                return
                
            hedge_btc_qty = delta * desired_qty  # Approximate hedge ratio
            
            logger.info(f"⚡ [TRIGGER] Executing Arb: Buy {desired_qty} YES @ {k_ask}c | Shorting {hedge_btc_qty:.5f} BTC via Futures")
            
            # Dispatch Async
            client_id = f"arb_{int(time.time())}"
            asyncio.create_task(self.kalshi.place_order(TARGET_KALSHI_MARKET, "buy", desired_qty, k_ask, client_id))
            asyncio.create_task(self.binance_rest.place_futures_order(BINANCE_SYMBOL, "SELL", hedge_btc_qty))
            
            # Update state
            self.deployed_capital += (desired_qty * k_ask / 100.0)
            self.active_positions += desired_qty
            
            # Wait briefly to avoid duplicate fires instantly
            time.sleep(1)

    async def heartbeat_loop(self):
        """Send a telegram heartbeat verifying the container is healthy every 6 hours."""
        from core.telegram import send_telegram_heartbeat, send_telegram_pnl
        while True:
            await send_telegram_heartbeat()
            # In production, pull exact PnL from SQLite db
            await send_telegram_pnl(self.deployed_capital, 0.0) 
            await asyncio.sleep(60 * 60 * 6)  # 6 Hours

    async def run(self):
        logger.info(f"Starting HFT Engine. Target: {TARGET_KALSHI_MARKET}")
        
        # Reconcile external state first
        await self.reconcile_positions()
        
        # Run websockets concurrently alongside heartbeat loop
        await asyncio.gather(
            self.binance_ws.connect(),
            self.kalshi.connect_ws([TARGET_KALSHI_MARKET], self.l2_store),
            self.heartbeat_loop()
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
