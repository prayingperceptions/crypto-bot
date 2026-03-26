import asyncio
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from core.logger import setup_logger
from core.kalshi_client import KalshiClient
from core.black_scholes import calculate_probability_above_strike

logger = setup_logger("market_scanner")

# Kalshi crypto series tickers (hourly snapshot events with tail markets)
CRYPTO_SERIES = {
    "KXBTCD": {"name": "BTC", "binance_symbol": "BTCUSDT"},
    "KXETHD": {"name": "ETH", "binance_symbol": "ETHUSDT"},
    "KXSOLD": {"name": "SOL", "binance_symbol": "SOLUSDT"},
    "KXXRP":  {"name": "XRP", "binance_symbol": "XRPUSDT"},
    "KXBNB":  {"name": "BNB", "binance_symbol": "BNBUSDT"},
    "KXHYPE": {"name": "HYPE", "binance_symbol": "HYPEUSDT"},
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

    async def _collect_candidates(self, spot_price: float, iv: float, 
                                    series_ticker: str, max_events: int = 5) -> List[Dict[str, Any]]:
        """Collect all tradeable tail market candidates across live events."""
        series_info = CRYPTO_SERIES.get(series_ticker, {"name": series_ticker, "binance_symbol": "BTCUSDT"})
        
        live_events = await self.discover_live_events(series_ticker)
        if not live_events:
            return []

        now = datetime.now(timezone.utc)
        candidates = []

        for event in live_events[:max_events]:
            event_ticker = event.get("event_ticker", "")
            markets = await self._fetch_event_markets(event_ticker)
            
            for market in markets:
                ticker = market.get("ticker", "")
                if "-T" not in ticker:
                    continue
                    
                floor_strike_raw = market.get("floor_strike")
                if floor_strike_raw is None:
                    continue
                strike = float(floor_strike_raw)
                if strike <= 0:
                    continue
                
                status = market.get("status", "")
                oi = float(market.get("open_interest_fp", "0") or "0")
                if oi < MIN_OPEN_INTEREST:
                    continue
                
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
                
                if days_to_expiry < (0.5 / 24.0):
                    continue
                
                fair_value = calculate_probability_above_strike(spot_price, strike, days_to_expiry, iv)
                fv_cents = int(fair_value * 100)
                
                if fv_cents < 5 or fv_cents > 95:
                    continue
                
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

        return candidates

    async def select_best_market(self, spot_price: float, iv: float = 50.0, 
                                  series_ticker: str = "KXBTCD") -> Optional[Dict[str, Any]]:
        """Find the single best market (closest to 50c fair value)."""
        candidates = await self._collect_candidates(spot_price, iv, series_ticker)
        if not candidates:
            logger.warning(f"No tradeable {series_ticker} tail markets found.")
            return None

        candidates.sort(key=lambda c: (c["distance_from_50"], -c["open_interest"]))
        best = candidates[0]
        logger.info(
            f"✅ Selected: {best['ticker']} | above ${best['strike']:,.0f} | "
            f"FV: {best['fair_value_cents']}c | OI: {best['open_interest']:,.0f} | "
            f"Exp: {best['days_to_expiry']*24:.1f}h"
        )
        return best

    async def select_top_n_markets(self, spot_price: float, iv: float = 50.0,
                                    series_ticker: str = "KXBTCD", 
                                    n: int = 3) -> List[Dict[str, Any]]:
        """
        Select the best market from each of the N nearest events.
        This gives us uncorrelated fill opportunities across different expiries.
        """
        candidates = await self._collect_candidates(spot_price, iv, series_ticker, max_events=n + 2)
        if not candidates:
            logger.warning(f"No tradeable {series_ticker} tail markets found.")
            return []

        # Group candidates by event_ticker
        by_event: Dict[str, List[Dict[str, Any]]] = {}
        for c in candidates:
            evt = c["event_ticker"]
            by_event.setdefault(evt, []).append(c)
        
        # From each event, pick the market closest to 50c
        selected = []
        for evt, markets in by_event.items():
            markets.sort(key=lambda c: (c["distance_from_50"], -c["open_interest"]))
            selected.append(markets[0])
        
        # Sort selected by soonest expiry first, take top N
        selected.sort(key=lambda c: c["days_to_expiry"])
        top_n = selected[:n]
        
        for i, m in enumerate(top_n):
            logger.info(
                f"✅ Market #{i+1}: {m['ticker']} | above ${m['strike']:,.0f} | "
                f"FV: {m['fair_value_cents']}c | OI: {m['open_interest']:,.0f} | "
                f"Exp: {m['days_to_expiry']*24:.1f}h"
            )
        
        return top_n

    async def _get_spot_price(self, binance_symbol: str) -> float:
        """Get spot price from Binance REST API for any crypto."""
        import aiohttp
        try:
            url = f"https://api.binance.us/api/v3/ticker/price?symbol={binance_symbol}"
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return float(data.get("price", 0))
        except Exception as e:
            logger.error(f"Failed to get {binance_symbol} price: {e}")
        return 0.0

    async def scan_all_cryptos(self, n: int = 3, 
                                series_list: list | None = None,
                                btc_iv: float = 50.0) -> List[Dict[str, Any]]:
        """
        Scan across all enabled crypto series and return the top N markets globally.
        Each crypto uses its own spot price from Binance.
        IV defaults to 50% for non-BTC (Deribit only has BTC/ETH DVOL).
        """
        if series_list is None:
            series_list = list(CRYPTO_SERIES.keys())
        
        all_candidates = []
        
        for series_ticker in series_list:
            info = CRYPTO_SERIES.get(series_ticker)
            if not info:
                continue
            
            # Get spot price for this crypto
            spot = await self._get_spot_price(info["binance_symbol"])
            if spot <= 0:
                logger.warning(f"No spot price for {info['name']}, skipping {series_ticker}")
                continue
            
            # Use BTC IV for BTC, default 50% for others (close enough for MM)
            iv = btc_iv if series_ticker == "KXBTCD" else 50.0
            
            candidates = await self._collect_candidates(spot, iv, series_ticker, max_events=3)
            all_candidates.extend(candidates)
            
            logger.info(f"  {info['name']}: {len(candidates)} tradeable markets (spot ${spot:,.2f})")
        
        if not all_candidates:
            logger.warning("No tradeable markets found across any crypto.")
            return []
        
        # Group by event_ticker, pick best per event
        by_event: Dict[str, List[Dict[str, Any]]] = {}
        for c in all_candidates:
            evt = c["event_ticker"]
            by_event.setdefault(evt, []).append(c)
        
        selected = []
        for evt, markets in by_event.items():
            markets.sort(key=lambda c: (c["distance_from_50"], -c["open_interest"]))
            selected.append(markets[0])
        
        # Sort by closest to 50c fair value, take top N
        selected.sort(key=lambda c: (c["distance_from_50"], -c["open_interest"]))
        top_n = selected[:n]
        
        for i, m in enumerate(top_n):
            logger.info(
                f"✅ Global #{i+1}: [{m['crypto_name']}] {m['ticker']} | "
                f"FV: {m['fair_value_cents']}c | OI: {m['open_interest']:,.0f} | "
                f"Exp: {m['days_to_expiry']*24:.1f}h"
            )
        
        return top_n

    async def close(self):
        await self.kalshi.close()
