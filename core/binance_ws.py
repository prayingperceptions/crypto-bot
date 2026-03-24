import asyncio
import json
import websockets
from typing import Callable, Optional
from core.logger import setup_logger

logger = setup_logger("binance_ws")

class BinanceWSClient:
    def __init__(self, symbol: str = "btcusdt"):
        self.symbol = symbol.lower()
        # Using bookTicker for lowest latency L1 best bid/ask updates from Binance US. 
        self.ws_url = f"wss://stream.binance.us:9443/ws/{self.symbol}@bookTicker"
        self.on_price_update: Optional[Callable[[float, float, float], None]] = None
        
    async def connect(self):
        logger.info(f"Connecting to Binance WebSocket at {self.ws_url}")
        while True:
            try:
                async with websockets.connect(self.ws_url) as ws:
                    logger.info("Connected to Binance WebSocket.")
                    async for message in ws:
                        if isinstance(message, str):
                            data = json.loads(message)
                            if "b" in data and "a" in data:
                                top_bid = float(data["b"])
                                top_ask = float(data.get('a'))
                                mid = (top_bid + top_ask) / 2
                                if self.on_price_update:
                                    self.on_price_update(mid, top_bid, top_ask)                                  # Need a sync wrapper or if on_price_update is async, await it.
                                    # Assuming synchronous callback for maximum performance to just update a shared state dict.
            except websockets.exceptions.ConnectionClosed:
                logger.warning("Binance WS closed. Reconnecting in 2 seconds...")
                await asyncio.sleep(2)
            except Exception as e:
                logger.error(f"Binance WS Error: {e}. Reconnecting...")
                await asyncio.sleep(2)
