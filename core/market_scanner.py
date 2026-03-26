import asyncio
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from core.logger import setup_logger
from core.kalshi_client import KalshiClient
from core.black_scholes import calculate_probability_above_strike

logger = setup_logger("market_scanner")

# Kalshi crypto series tickers (hourly snapshot events with tail markets)
# KXBTCD = BTC daily/hourly tails, KXETHD = ETH, etc.
CRYPTO_SERIES = {
    "KXBTCD": {"name": "BTC", "binance_symbol": "BTCUSDT"},
    # Future expansion:
    # "KXETHD": {"name": "ETH", "binance_symbol": "ETHUSDT"},
    # "KXSOLD": {"name": "SOL", "binance_symbol": "SOLUSDT"},
}

# Minimum open interest to consider a market (proxy for volume)
MIN_OPEN_INTEREST = 100

class MarketScanner:
    def __init__(self, kalshi_client: Optional[KalshiClient] = None):
        self.kalshi = kalshi_client or KalshiClient(is_demo=False)
        self.active_crypto_markets: List[Dict[str, Any]] = []

    async def _fetch_events(self, series_ticker: str, limit: int = 20) -> List[Dict[str, Any]]:
        """Fetch events for a given series ticker."""
        import urllib.parse
        path = f"/events?series_ticker={series_ticker}&limit={limit}"
        return (await self.kalshi._request("GET", path)).get("events", [])

    async def _fetch_event_markets(self, event_ticker: str) -> List[Dict[str, Any]]:
        """Fetch all markets within an event (handles pagination)."""
        all_markets = []
        cursor = ""
        for _ in range(5):
            params = f"event_ticker={event_ticker}&limit=200"
            if cursor:
                params += f"&cursor={cursor}"
            resp = await self.kalshi._request("GET", f"/markets?{params}")
            markets = resp.get("markets", [])
            all_markets.extend(markets)
            cursor = resp.get("cursor", "")
            if not cursor or not markets:
                break
        return all_markets

    async def discover_live_events(self, series_ticker: str = "KXBTCD") -> List[Dict[str, Any]]:
        """
        Discover currently live/upcoming events for a crypto series.
        Returns events sorted by closest expiry first.
        """
        logger.info(f"Scanning Kalshi for live {series_ticker} events...")
        
        try:
            events = await self._fetch_events(series_ticker, limit=50)
            now = datetime.now(timezone.utc)
            
            live_events = []
            for ev in events:
                strike_date_str = ev.get("strike_date", "")
                if strike_date_str:
                    try:
                        strike_dt = datetime.fromisoformat(strike_date_str.replace("Z", "+00:00"))
                        hours_left = (strike_dt - now).total_seconds() / 3600.0
                        # Include events with 30min to 48h remaining
                        # Active markets live in the 0.5-6h window typically
                        if 0.5 <= hours_left <= 48.0:
                            ev["_strike_dt"] = strike_dt
                            ev["_hours_to_expiry"] = hours_left
                            live_events.append(ev)
                    except Exception:
                        pass
            
            # Sort by soonest tradeable expiry first
            live_events.sort(key=lambda e: e["_hours_to_expiry"])
            logger.info(f"Found {len(live_events)} live/upcoming {series_ticker} events.")
            return live_events
            
        except Exception as e:
            logger.error(f"Failed to discover events: {e}")
            return []

    async def select_best_market(self, spot_price: float, iv: float = 50.0, 
                                  series_ticker: str = "KXBTCD") -> Optional[Dict[str, Any]]:
        """
        Find the best market to trade by:
        1. Scanning live events for the given series
        2. Fetching all tail markets within each event
        3. Picking the tail with fair value closest to 50c AND sufficient open interest
        
        Returns dict with: ticker, strike, close_time, days_to_expiry, expiry_dt,
        fair_value_cents, open_interest, binance_symbol
        """
        series_info = CRYPTO_SERIES.get(series_ticker, {"name": series_ticker, "binance_symbol": "BTCUSDT"})
        
        live_events = await self.discover_live_events(series_ticker)
        if not live_events:
            logger.warning(f"No live events found for {series_ticker}.")
            return None

        now = datetime.now(timezone.utc)
        candidates = []

        # Check the first 5 soonest events (more events = more chances)
        for event in live_events[:5]:
            event_ticker = event.get("event_ticker", "")
            markets = await self._fetch_event_markets(event_ticker)
            
            for market in markets:
                ticker = market.get("ticker", "")
                
                # Only tail markets (-T) with floor_strike
                if "-T" not in ticker:
                    continue
                    
                floor_strike_raw = market.get("floor_strike")
                if floor_strike_raw is None:
                    continue
                strike = float(floor_strike_raw)
                if strike <= 0:
                    continue
                
                # Accept any status (active, initialized, open)
                status = market.get("status", "")
                
                # Open interest filter (0 OI = no liquidity at all)
                oi = float(market.get("open_interest_fp", "0") or "0")
                if oi < MIN_OPEN_INTEREST:
                    continue
                
                # Parse close_time for expiry
                close_time_str = market.get("close_time") or market.get("expected_expiration_time")
                days_to_expiry = 0.0
                expiry_dt = None
                if close_time_str:
                    try:
                        expiry_dt = datetime.fromisoformat(close_time_str.replace("Z", "+00:00"))
                        delta = expiry_dt - now
                        days_to_expiry = max(delta.total_seconds() / 86400.0, 0.0)
                    except Exception:
                        pass
                
                if days_to_expiry < (0.5 / 24.0):  # Skip if < 30 min to expiry
                    continue
                
                # Fair value from Black-Scholes
                fair_value = calculate_probability_above_strike(spot_price, strike, days_to_expiry, iv)
                fv_cents = int(fair_value * 100)
                
                # Only consider markets with tradeable fair value (5-95c)
                if fv_cents < 5 or fv_cents > 95:
                    continue
                
                # Bid/ask from API (for reference)
                yes_bid = float(market.get("previous_yes_bid_dollars", "0") or "0")
                yes_ask = float(market.get("previous_yes_ask_dollars", "0") or "0")
                
                distance_from_50 = abs(fv_cents - 50)
                
                candidates.append({
                    "ticker": ticker,
                    "strike": strike,
                    "market_type": "tail",
                    "close_time": close_time_str,
                    "expiry_dt": expiry_dt,
                    "days_to_expiry": days_to_expiry,
                    "fair_value_cents": fv_cents,
                    "distance_from_50": distance_from_50,
                    "open_interest": oi,
                    "status": status,
                    "yes_bid": yes_bid,
                    "yes_ask": yes_ask,
                    "event_ticker": event_ticker,
                    "binance_symbol": series_info["binance_symbol"],
                    "crypto_name": series_info["name"],
                })

        if not candidates:
            logger.warning(f"No tradeable {series_ticker} tail markets found with OI >= {MIN_OPEN_INTEREST}.")
            return None

        # Sort by: closest to 50c fair value, then highest OI
        candidates.sort(key=lambda c: (c["distance_from_50"], -c["open_interest"]))
        
        best = candidates[0]
        logger.info(
            f"✅ Selected: {best['ticker']} | above ${best['strike']:,.0f} | "
            f"FV: {best['fair_value_cents']}c | OI: {best['open_interest']:,.0f} | "
            f"Exp: {best['days_to_expiry']*24:.1f}h | Bid: {best['yes_bid']*100:.0f}c Ask: {best['yes_ask']*100:.0f}c"
        )
        
        for i, c in enumerate(candidates[:5]):
            logger.debug(
                f"  #{i+1}: {c['ticker']} fv={c['fair_value_cents']}c "
                f"oi={c['open_interest']:,.0f} exp={c['days_to_expiry']*24:.1f}h"
            )
        
        return best

    async def close(self):
        await self.kalshi.close()
