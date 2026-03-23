import aiosqlite
import logging
from core.logger import setup_logger

logger = setup_logger("db")

class TradeLedger:
    def __init__(self, db_path: str = "crypto_bot.db"):
        self.db_path = db_path
        
    async def initialize(self):
        """Creates the necessary tables if they don't exist yet."""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                # Active positions mapping
                await db.execute('''
                    CREATE TABLE IF NOT EXISTS positions (
                        market_ticker TEXT PRIMARY KEY,
                        qty INTEGER,
                        avg_entry_cents REAL,
                        status TEXT
                    )
                ''')
                # Trade audit log
                await db.execute('''
                    CREATE TABLE IF NOT EXISTS orders (
                        client_order_id TEXT PRIMARY KEY,
                        market_ticker TEXT,
                        action TEXT,
                        qty INTEGER,
                        price_cents INTEGER,
                        status TEXT,
                        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                await db.commit()
                logger.info("SQLite Trade Ledger initialized.")
        except Exception as e:
            logger.error(f"Failed to config SQLite: {e}")

    async def log_order(self, client_order_id: str, market: str, action: str, qty: int, price: int):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('''
                INSERT OR REPLACE INTO orders (client_order_id, market_ticker, action, qty, price_cents, status)
                VALUES (?, ?, ?, ?, ?, 'submitted')
            ''', (client_order_id, market, action, qty, price))
            await db.commit()
            
    async def set_position(self, market: str, qty: int, avg_entry: float = 0.0):
        """Update or insert a position state."""
        async with aiosqlite.connect(self.db_path) as db:
            if qty <= 0:
                await db.execute('DELETE FROM positions WHERE market_ticker = ?', (market,))
            else:
                await db.execute('''
                    INSERT OR REPLACE INTO positions (market_ticker, qty, avg_entry_cents, status)
                    VALUES (?, ?, ?, 'open')
                ''', (market, qty, avg_entry))
            await db.commit()

    async def get_active_positions(self) -> dict:
        """Returns a dict of {market_ticker: qty} for tracking running exposure."""
        positions = {}
        try:
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute('SELECT market_ticker, qty FROM positions WHERE qty > 0') as cursor:
                    async for row in cursor:
                        positions[row[0]] = row[1]
        except Exception as e:
            logger.error(f"DB Read Error: {e}")
        return positions
