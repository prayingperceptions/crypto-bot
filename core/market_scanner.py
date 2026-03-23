import asyncio
from typing import List, Dict, Any
from core.logger import setup_logger
from core.kalshi_client import KalshiClient

logger = setup_logger("market_scanner")

class MarketScanner:
    def __init__(self):
        self.kalshi = KalshiClient(is_demo=True)
        self.active_crypto_markets = []

    async def discover_markets(self) -> List[Dict[str, Any]]:
        """
        Fetch active BTC and ETH prediction markets from Kalshi 
        (e.g., Daily/Weekly 'Will BTC reach X?').
        """
        logger.info("Scanning Kalshi for active crypto markets...")
        
        try:
            # Series tickers logic: e.g. KXBTCUSD limits to BTC/USD markets
            response = await self.kalshi.get_markets(series_ticker="KXBTCUSD", status="active", limit=100)
            
            if "markets" in response:
                markets = response["markets"]
                self.active_crypto_markets = markets
                logger.info(f"Discovered {len(markets)} active Kalshi BTC markets.")
                return markets
            else:
                logger.warning(f"Unexpected response from /markets: {response}")
                return []
        except Exception as e:
            logger.error(f"Failed to scan markets: {e}")
            return []
            
    def map_market_to_strike(self, market_ticker: str) -> float:
        """
        Kalshi BTC tickers typically look like: KXBTCUSD-24M01-100000
        Extracts 100000 as the strike price.
        """
        try:
            parts = market_ticker.split('-')
            if len(parts) >= 3:
                # E.g., 100000 
                return float(parts[2])
        except Exception:
            pass
        return 0.0

    async def close(self):
        await self.kalshi.close()
