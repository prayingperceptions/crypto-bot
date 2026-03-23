import os
import hmac
import hashlib
import time
import aiohttp
from typing import Dict, Any
from core.logger import setup_logger

logger = setup_logger("binance_rest")

class BinanceFuturesClient:
    def __init__(self, testnet: bool = False):
        self.api_key = os.getenv("BINANCE_API_KEY")
        self.api_secret = os.getenv("BINANCE_API_SECRET")
        self.base_url = "https://testnet.binancefuture.com" if testnet else "https://fapi.binance.com"
        
    def _generate_signature(self, query_string: str) -> str:
        if not self.api_secret:
            raise ValueError("BINANCE_API_SECRET missing.")
        return hmac.new(
            self.api_secret.encode('utf-8'),
            query_string.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()

    async def place_futures_order(self, symbol: str, side: str, quantity: float, order_type: str = "MARKET") -> Dict[str, Any]:
        """
        Place an order on Binance Futures (for Delta Hedging).
        side: 'BUY' or 'SELL'
        """
        if not self.api_key or not self.api_secret:
            logger.error("Binance APIs missing. Cannot hedge!")
            return {"error": "Missing keys"}
            
        endpoint = "/fapi/v1/order"
        timestamp = int(time.time() * 1000)
        
        params = {
            "symbol": symbol.upper(),
            "side": side.upper(),
            "type": order_type.upper(),
            "quantity": f"{quantity:.5f}",
            "timestamp": timestamp
        }
        
        import urllib.parse
        query_string = urllib.parse.urlencode(params)
        signature = self._generate_signature(query_string)
        
        url = f"{self.base_url}{endpoint}?{query_string}&signature={signature}"
        
        headers = {
            "X-MBX-APIKEY": self.api_key
        }
        
        logger.info(f"Submitting Binance Hedge Order: {side} {quantity} {symbol}")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers) as response:
                    data = await response.json()
                    if not response.ok:
                        logger.error(f"Binance API Error: {response.status} {data}")
                    return dict(data)
        except Exception as e:
            logger.error(f"Failed to place Binance order: {e}")
            return {"error": str(e)}
