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

# ─── Capital Tier System ────────────────────────────────────────────
# The bot auto-scales based on portfolio balance.
# More capital = more markets + cryptos = more fill opportunities.
CAPITAL_TIERS = [
    # (min_balance, max_markets, crypto_series_to_scan)
    (0,    1, ["KXBTCD"]),                                              # $0-99:    1 BTC market
    (100,  3, ["KXBTCD", "KXETHD"]),                                    # $100-499: 3 markets, BTC+ETH
    (500,  5, ["KXBTCD", "KXETHD", "KXSOLD", "KXXRP"]),                # $500-2499: 5 markets, 4 cryptos
    (2500, 8, ["KXBTCD", "KXETHD", "KXSOLD", "KXXRP", "KXBNB", "KXHYPE"]),  # $2500+: 8 markets, all cryptos
]

MAX_SLIPPAGE_CENTS = 2
TRADE_FRACTION = 0.10  # 10% of balance per trade per market
MARKET_RESCAN_INTERVAL = 900  # 15 min

class ActiveMarket:
    """Tracks state for one market the engine is trading on."""
    def __init__(self, ticker: str, strike: float, expiry_dt: datetime | None,
                 fair_value_cents: int = 0, event_ticker: str = "",
                 binance_symbol: str = "BTCUSDT", crypto_name: str = "BTC"):
        self.ticker = ticker
        self.strike = strike
        self.expiry_dt = expiry_dt
        self.event_ticker = event_ticker
        self.last_fair_value = fair_value_cents
        self.active_positions = 0
        self.binance_symbol = binance_symbol
        self.crypto_name = crypto_name
    
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
        
        # Per-crypto Binance WS feeds (keyed by symbol like "BTCUSDT")
        self.binance_feeds: dict[str, BinanceWSClient] = {}
        
        # Active markets (multiple, across different events/cryptos)
        self.markets: list[ActiveMarket] = []
        
        # Shared state
        self.iv: float = 50.0
        self.max_capital: float = 0.0
        self.deployed_capital = 0.0
        
        # Current tier settings
        self.tier_max_markets: int = 1
        self.tier_series: list[str] = ["KXBTCD"]
        
        # Rolling price history for dynamic spread (keyed by market ticker)
        self._price_history: dict[str, list[tuple[float, float]]] = {}

    def _get_tier(self) -> tuple[int, list[str]]:
        """Determine current capital tier based on portfolio balance."""
        max_markets = 1
        series = ["KXBTCD"]
        for min_bal, markets, cryptos in CAPITAL_TIERS:
            if self.max_capital >= min_bal:
                max_markets = markets
                series = cryptos
        return max_markets, series

    def _get_binance_ws(self, symbol: str) -> BinanceWSClient:
        """Get or create Binance WS for a given symbol."""
        key = symbol.lower()
        if key not in self.binance_feeds:
            ws = BinanceWSClient(symbol=symbol)
            ws.on_price_update = lambda mid, bid, ask, sym=key: self._on_price(sym, mid, bid, ask)
            self.binance_feeds[key] = ws
        return self.binance_feeds[key]

    def _on_price(self, symbol: str, mid: float, bid: float, ask: float):
        """Called on every Binance tick. Evaluates markets for this crypto."""
        for market in self.markets:
            if market.binance_symbol.lower() != symbol:
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
            logger.warning(f"🚨 [STOP LOSS] {market.ticker} | FV: {fair_value_cents}c")
            qty_to_sell = min(market.active_positions, k_bid_qty)
            if qty_to_sell > 0:
                client_id = f"sl_{market.ticker[-8:]}_{int(time.time())}"
                asyncio.create_task(self.kalshi.place_order(market.ticker, "sell", qty_to_sell, k_bid, client_id, order_type="market"))
                market.active_positions -= qty_to_sell
                time.sleep(1)
                
    async def reconcile_positions(self):
        """Fetch currently open positions on boot."""
        logger.info("Reconciling positions...")
        resp = await self.kalshi.get_positions()
        if "positions" in resp:
            for position in resp["positions"]:
                for market in self.markets:
                    if position.get("ticker") == market.ticker:
                        market.active_positions = position.get("position", 0)
        total = sum(m.active_positions for m in self.markets)
        logger.info(f"Reconciled: {total} positions across {len(self.markets)} markets")
        
    def _compute_fair_value_cents(self, market: ActiveMarket, spot: float) -> int | None:
        days = market.get_days_to_expiry()
        if days <= 0 or market.strike <= 0:
            return None
        prob = calculate_probability_above_strike(spot, market.strike, days, self.iv)
        return int(prob * 100)

    async def fetch_balance(self):
        """Fetch live portfolio balance from Kalshi and update tier."""
        try:
            resp = await self.kalshi.get_balance()
            balance = float(resp.get("balance", 0))
            if balance > 100:
                balance = balance / 100.0
            if balance > 0:
                self.max_capital = balance
                
                # Update tier
                old_tier = (self.tier_max_markets, self.tier_series)
                self.tier_max_markets, self.tier_series = self._get_tier()
                new_tier = (self.tier_max_markets, self.tier_series)
                
                cryptos_str = ", ".join(self.tier_series)
                logger.info(f"💰 Balance: ${self.max_capital:.2f} | Tier: {self.tier_max_markets} markets, [{cryptos_str}]")
                
                if new_tier != old_tier and old_tier[0] > 0:
                    logger.info(f"📈 TIER UPGRADE! Now trading {self.tier_max_markets} markets across {len(self.tier_series)} cryptos")
            else:
                logger.warning(f"Balance unavailable, using: ${self.max_capital:.2f}")
        except Exception as e:
            logger.error(f"Balance fetch failed: {e}")

    def _compute_dynamic_spread(self, market: ActiveMarket, spot: float) -> int:
        """
        Dynamic spread that widens based on:
        1. Time to expiry (closer = more gamma risk = wider spread)
        2. Recent price volatility (more volatile = wider spread)
        Returns spread in cents per side (e.g., 2 = bid at FV-2, ask at FV+2).
        """
        base_spread = 2  # Default 2¢ per side
        
        # 1. Gamma adjustment: widen near expiry (last 30 min is dangerous)
        hours_left = market.get_days_to_expiry() * 24
        if hours_left < 0.25:      # < 15 min
            base_spread += 3
        elif hours_left < 0.5:     # < 30 min
            base_spread += 2
        elif hours_left < 1.0:     # < 1 hour
            base_spread += 1
        
        # 2. Volatility adjustment: track recent price moves
        now = time.time()
        key = market.ticker
        if key not in self._price_history:
            self._price_history[key] = []
        
        self._price_history[key].append((now, spot))
        # Keep last 5 minutes only
        cutoff = now - 300
        self._price_history[key] = [(t, p) for t, p in self._price_history[key] if t > cutoff]
        
        prices = [p for _, p in self._price_history[key]]
        if len(prices) >= 2:
            pct_move = abs(max(prices) - min(prices)) / min(prices) * 100
            if pct_move > 1.0:      # > 1% in 5 min = very volatile
                base_spread += 3
            elif pct_move > 0.5:    # > 0.5%
                base_spread += 2
            elif pct_move > 0.2:    # > 0.2%
                base_spread += 1
        
        return min(base_spread, 8)  # Cap at 8¢ per side

    def evaluate_trade_trigger(self, market: ActiveMarket, binance_price: float, 
                                k_bid: int, k_bid_qty: int, k_ask: int, k_ask_qty: int):
        fair_value_cents = self._compute_fair_value_cents(market, binance_price)
        if fair_value_cents is None:
            return
        
        spread = self._compute_dynamic_spread(market, binance_price)
        my_bid = max(1, fair_value_cents - spread)
        my_ask = min(99, fair_value_cents + spread)
        
        if abs(fair_value_cents - market.last_fair_value) >= 1:
            hours_left = market.get_days_to_expiry() * 24
            logger.info(
                f"🔄 [{market.crypto_name}] {market.ticker} | FV: {fair_value_cents}c | "
                f"Spread: ±{spread}c | Exp: {hours_left:.1f}h -> BID: {my_bid}c | ASK: {my_ask}c"
            )
            market.last_fair_value = fair_value_cents
            
            capital_per_market = self.max_capital / max(len(self.markets), 1)
            target_trade_usd = capital_per_market * TRADE_FRACTION
            if target_trade_usd <= 0:
                return
            
            max_contracts_bid = int((target_trade_usd * 100) / my_bid) if my_bid > 0 else 0
            max_contracts_ask = int((target_trade_usd * 100) / my_ask) if my_ask > 0 else 0
            
            if max_contracts_bid <= 0 and max_contracts_ask <= 0:
                return
            
            # Cancel stale orders, then post new ones
            async def _cancel_and_replace():
                await self.kalshi.cancel_orders_for_market(market.ticker)
                
                client_id_buy = f"mm_b_{market.ticker[-8:]}_{int(time.time())}"
                client_id_sell = f"mm_s_{market.ticker[-8:]}_{int(time.time())}"
                
                await self.kalshi.place_order(market.ticker, "buy", max_contracts_bid, my_bid, client_id_buy, order_type="limit")
                await self.kalshi.place_order(market.ticker, "sell", max_contracts_ask, my_ask, client_id_sell, order_type="limit")
            
            asyncio.create_task(_cancel_and_replace())

    async def discover_and_set_markets(self):
        """Discover top N markets across all tier-enabled cryptos."""
        from core.telegram import send_telegram_market_switch
        
        self.iv = await get_btc_dvol()
        
        # Use multi-crypto scanner with tier-appropriate series
        top_markets = await self.scanner.scan_all_cryptos(
            n=self.tier_max_markets,
            series_list=self.tier_series,
            btc_iv=self.iv
        )
        
        if not top_markets:
            # Fallback to BTC-only scan
            spot = await get_btc_price()
            if spot > 0:
                top_markets = await self.scanner.select_top_n_markets(spot, iv=self.iv, n=self.tier_max_markets)
        
        if not top_markets:
            logger.error("No suitable markets found across any crypto.")
            return False
        
        old_tickers = set(m.ticker for m in self.markets)
        new_tickers = set(m["ticker"] for m in top_markets)
        
        # Cancel all orders on markets we're leaving
        retired_tickers = old_tickers - new_tickers
        for ticker in retired_tickers:
            try:
                await self.kalshi.cancel_orders_for_market(ticker)
            except Exception:
                pass
        
        # Build new market objects
        new_market_objs = []
        needed_symbols = set()
        for mkt in top_markets:
            sym = mkt.get("binance_symbol", "BTCUSDT")
            needed_symbols.add(sym)
            new_market_objs.append(ActiveMarket(
                ticker=mkt["ticker"],
                strike=mkt["strike"],
                expiry_dt=mkt.get("expiry_dt"),
                fair_value_cents=mkt.get("fair_value_cents", 0),
                event_ticker=mkt.get("event_ticker", ""),
                binance_symbol=sym,
                crypto_name=mkt.get("crypto_name", "BTC"),
            ))
        
        self.markets = new_market_objs
        
        # Initialize Binance WS feeds for all needed crypto symbols
        for sym in needed_symbols:
            self._get_binance_ws(sym)
        
        # Switch Kalshi WS subscriptions incrementally
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
        
        # Log summary
        for mkt in self.markets:
            logger.info(
                f"✅ [{mkt.crypto_name}] {mkt.ticker} | ${mkt.strike:,.0f} | "
                f"FV: {mkt.last_fair_value}c | Exp: {mkt.get_days_to_expiry()*24:.1f}h"
            )
        
        if new_tickers != old_tickers:
            market_list = " | ".join(f"[{m.crypto_name}] {m.ticker} ({m.last_fair_value}c)" for m in self.markets)
            await send_telegram_market_switch(
                market_list, 
                self.markets[0].strike,
                self.markets[0].get_days_to_expiry(),
                self.markets[0].last_fair_value
            )
        
        cap_per = self.max_capital / max(len(self.markets), 1)
        logger.info(f"💰 ${self.max_capital:.2f} total | ${cap_per:.2f}/market | {len(self.markets)} markets | {len(needed_symbols)} cryptos")
        return True

    async def market_rescan_loop(self):
        """Periodically re-scan for better markets."""
        while True:
            await asyncio.sleep(MARKET_RESCAN_INTERVAL)
            logger.info(f"🔎 Market re-scan ({MARKET_RESCAN_INTERVAL//60}min)...")
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
            market_summary = ", ".join(f"[{m.crypto_name}]{m.ticker}({m.last_fair_value}c)" for m in self.markets) or "none"
            await send_telegram_heartbeat(market_summary, len(self.markets))
            await send_telegram_pnl(self.deployed_capital, 0.0) 
            await asyncio.sleep(60 * 60 * 6)

    async def run(self):
        # 1. Fetch balance and determine tier
        await self.fetch_balance()
        if self.max_capital <= 0:
            logger.error("FATAL: Portfolio balance is $0.")
            return
        
        logger.info(
            f"Starting HFT Engine | Balance: ${self.max_capital:.2f} | "
            f"Tier: {self.tier_max_markets} markets, {self.tier_series}"
        )
        
        # 2. Discover markets using tier-appropriate crypto list
        success = await self.discover_and_set_markets()
        if not success:
            logger.error("FATAL: No active markets found.")
            return
        
        # 3. Reconcile positions
        await self.reconcile_positions()
        
        # 4. Run all event loops
        all_tickers = [m.ticker for m in self.markets]
        
        # Gather all coroutines: Binance WS per crypto + Kalshi WS + loops
        tasks = [
            self.kalshi.connect_ws(all_tickers, self.l2_store),
            self.heartbeat_loop(),
            self.market_rescan_loop(),
            self.iv_and_balance_refresh_loop(),
        ]
        
        # Add one Binance WS per needed crypto symbol
        for sym, ws in self.binance_feeds.items():
            tasks.append(ws.connect())
        
        cryptos = set(m.crypto_name for m in self.markets)
        logger.info(
            f"Launching {len(tasks)} tasks | {len(self.markets)} markets | "
            f"Cryptos: {', '.join(cryptos)} | Capital: ${self.max_capital:.2f}"
        )
        await asyncio.gather(*tasks)

async def main():
    load_dotenv()
    engine = HftEngine()
    try:
        await engine.run()
    except KeyboardInterrupt:
        logger.info("Shutting down.")
    finally:
        await engine.kalshi.close()

if __name__ == "__main__":
    asyncio.run(main())
