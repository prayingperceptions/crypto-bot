from typing import Dict, List, Tuple
from core.logger import setup_logger

logger = setup_logger("kalshi_l2")

class OrderBookStore:
    def __init__(self):
        # Maps market_ticker -> { "bids": { price: depth }, "asks": { price: depth } }
        self.books: Dict[str, Dict[str, Dict[int, int]]] = {}
        
    def process_snapshot(self, market_ticker: str, bids: List[Tuple[int, int]], asks: List[Tuple[int, int]]):
        """Process a full orderbook snapshot. Prices and depths are integers (cents)."""
        self.books[market_ticker] = {
            "bids": {price: depth for price, depth in bids},
            "asks": {price: depth for price, depth in asks}
        }
        logger.info(f"Loaded L2 Snapshot for {market_ticker} (Bids: {len(bids)}, Asks: {len(asks)})")

    def process_delta(self, market_ticker: str, bids_delta: List[Tuple[int, int]], asks_delta: List[Tuple[int, int]]):
        """Process orderbook_delta modifications."""
        if market_ticker not in self.books:
            # Drop delta if snapshot not yet received
            return
            
        book = self.books[market_ticker]
        
        self._apply_level_deltas(book["bids"], bids_delta)
        self._apply_level_deltas(book["asks"], asks_delta)

    def _apply_level_deltas(self, target_side: Dict[int, int], deltas: List[Tuple[int, int]]):
        for price, diff in deltas:
            current = target_side.get(price, 0)
            new_depth = current + diff
            if new_depth <= 0:
                if price in target_side:
                    del target_side[price]
            else:
                target_side[price] = new_depth

    def get_top_of_book(self, market_ticker: str) -> Tuple[int, int, int, int]:
        """Returns (best_bid_price, best_bid_qty, best_ask_price, best_ask_qty)"""
        if market_ticker not in self.books:
            return (0, 0, 0, 0)
            
        book = self.books[market_ticker]
        bids = book["bids"]
        asks = book["asks"]
        
        best_bid = max(bids.keys()) if bids else 0
        best_bid_qty = bids[best_bid] if best_bid else 0
        
        best_ask = min(asks.keys()) if asks else 0
        best_ask_qty = asks[best_ask] if best_ask else 0
        
        return (best_bid, best_bid_qty, best_ask, best_ask_qty)
