import os
import time
import base64
import asyncio
import aiohttp
import websockets
import json
from typing import Optional, Dict, Any, List

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from core.logger import setup_logger

logger = setup_logger("kalshi_client")

class KalshiClient:
    def __init__(self, is_demo: bool = True):
        self.api_key = os.getenv("KALSHI_API_KEY")
        private_key_input = os.getenv("KALSHI_PRIVATE_KEY")
        
        self.base_url = "https://demo-api.kalshi.co/trade-api/v2" if is_demo else "https://api.elections.kalshi.com/trade-api/v2"
        self.ws_url = "wss://demo-api.kalshi.co/trade-api/ws/v2" if is_demo else "wss://api.elections.kalshi.com/trade-api/ws/v2"
        
        if not self.api_key or not private_key_input:
            logger.warning("KALSHI_API_KEY or KALSHI_PRIVATE_KEY is missing from environment. Client cannot authenticate.")
            self.private_key = None
        else:
            self.private_key = self._load_private_key(str(private_key_input))
            
        self.session: Optional[aiohttp.ClientSession] = None
        
    def _load_private_key(self, key_input: str):
        try:
            if os.path.isfile(key_input):
                with open(key_input, "rb") as f:
                    key_data = f.read()
            else:
                key_data = key_input.encode('utf-8')
                
            key_data = key_data.replace(b'\\n', b'\n')
            return load_pem_private_key(key_data, password=None)
        except Exception as e:
            logger.error(f"Failed to load private key: {e}")
            return None
            
    def _generate_signature(self, timestamp: int, method: str, path: str) -> str:
        if not self.private_key:
            raise ValueError("Private key is not loaded")
            
        msg_string = f"{timestamp}{method}{path}"
        msg_bytes = msg_string.encode('utf-8')
        
        pkey: Any = self.private_key
        signature = pkey.sign(
            msg_bytes,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH
            ),
            hashes.SHA256()
        )
        return base64.b64encode(signature).decode('utf-8')

    def _get_auth_headers(self, method: str, path: str) -> Dict[str, str]:
        current_time_milliseconds = int(time.time() * 1000)
        try:
            sig = self._generate_signature(current_time_milliseconds, method, path)
            return {
                "KALSHI-ACCESS-KEY": self.api_key or "",
                "KALSHI-ACCESS-SIGNATURE": sig,
                "KALSHI-ACCESS-TIMESTAMP": str(current_time_milliseconds)
            }
        except ValueError as e:
            logger.error(f"Cannot generate headers: {e}")
            return {}

    async def _request(self, method: str, path: str, **kwargs) -> Dict[str, Any]:
        if not self.session:
            self.session = aiohttp.ClientSession()
        session = self.session
            
        headers = kwargs.pop("headers", {})
        auth_headers = self._get_auth_headers(method, path)
        headers.update(auth_headers)
        
        url = f"{self.base_url}{path}"
        max_retries = 3
        backoff = 1.0

        for attempt in range(max_retries):
            async with session.request(method, url, headers=headers, **kwargs) as response:
                if response.status == 429:
                    logger.warning(f"Rate limited (429) on Kalshi API. Backing off for {backoff} seconds... (Attempt {attempt+1}/{max_retries})")
                    await asyncio.sleep(backoff)
                    backoff *= 2.0
                    continue

                try:
                    data = await response.json()
                    if not response.ok:
                        logger.error(f"Kalshi API Error: {response.status} {data}")
                    return data
                except Exception as e:
                    text = await response.text()
                    logger.error(f"Failed to parse Kalshi API response: {e}. Text: {text}")
                    return {"error": text}
        
        return {"error": "Max retries exceeded on 429"}
                
    async def get_markets(self, ticker: Optional[str] = None, **params) -> Dict[str, Any]:
        """Fetch markets from Kalshi via REST."""
        path = "/markets"
        if ticker:
            path = f"/markets/{ticker}"
        if params:
            import urllib.parse
            path += f"?{urllib.parse.urlencode(params)}"
            
        return await self._request("GET", path)

    async def get_balance(self) -> Dict[str, Any]:
        """Fetch account balance."""
        return await self._request("GET", "/portfolio/balance")

    async def get_positions(self) -> Dict[str, Any]:
        """Fetch active portfolio positions."""
        return await self._request("GET", "/portfolio/positions")

    async def place_order(self, ticker: str, action: str, count: int, yes_price: int, client_order_id: str, order_type: str = "limit") -> Dict[str, Any]:
        """
        Place an order on Kalshi.
        action: 'buy' or 'sell'
        count: number of contracts
        yes_price: price in cents (1-99)
        order_type: 'market' or 'limit'
        """
        path = "/portfolio/orders"
        payload = {
            "ticker": ticker,
            "client_order_id": client_order_id,
            "action": action.lower(),
            "type": order_type.lower(),
            "yes_price": int(yes_price),
            "count": int(count)
        }
        logger.info(f"Submitting Kalshi Order: {payload}")
        return await self._request("POST", path, json=payload)

    async def connect_ws(self, market_tickers: List[str], l2_store: Any):
        """Connect to the generic Kalshi WebSocket (Phase 2)."""
        logger.info(f"Connecting to Kalshi WebSocket at {self.ws_url}")
        try:
            async with websockets.connect(self.ws_url) as ws:
                current_time_milliseconds = int(time.time() * 1000)
                sig = self._generate_signature(current_time_milliseconds, "GET", "/trade-api/ws/v2")
                
                auth_msg = {
                    "id": 1,
                    "cmd": "subscribe",
                    "params": {
                        "channels": ["auth"],
                        "kalshi-access-key": self.api_key or "",
                        "kalshi-access-signature": sig,
                        "kalshi-access-timestamp": current_time_milliseconds
                    }
                }
                await ws.send(json.dumps(auth_msg))
                
                sub_msg = {
                    "id": 2,
                    "cmd": "subscribe",
                    "params": {
                        "channels": ["orderbook_delta"],
                        "market_tickers": market_tickers
                    }
                }
                await ws.send(json.dumps(sub_msg))
                
                async for message in ws:
                    if isinstance(message, str):
                        data = json.loads(message)
                        msg_type = data.get("type")
                        msg_data = data.get("msg", {})
                        
                        if msg_type == "orderbook_snapshot":
                            ticker = msg_data.get("market_ticker")
                            bids = msg_data.get("bids", [])
                            asks = msg_data.get("asks", [])
                            if ticker:
                                l2_store.process_snapshot(ticker, bids, asks)
                                
                        elif msg_type == "orderbook_delta":
                            ticker = msg_data.get("market_ticker")
                            bids = msg_data.get("bids", [])
                            asks = msg_data.get("asks", [])
                            if ticker:
                                l2_store.process_delta(ticker, bids, asks)
                                
        except Exception as e:
            logger.error(f"WebSocket error: {e}")

    async def close(self):
        session = self.session
        if session:
            await session.close()
