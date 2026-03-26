import aiohttp
import time
import logging

logger = logging.getLogger(__name__)

# Fallback IV if Deribit is unreachable
DEFAULT_IV = 50.0

async def get_btc_dvol() -> float:
    """
    Fetch the live Bitcoin Implied Volatility (DVOL) from Deribit.
    Returns the annualized implied volatility (e.g., 55.4 means 55.4%).
    Falls back to DEFAULT_IV if the API is unreachable.
    """
    now_ms = int(time.time() * 1000)
    url = (
        f"https://deribit.com/api/v2/public/get_volatility_index_data"
        f"?currency=BTC&resolution=3600"
        f"&start_timestamp={now_ms - 7200000}"  # 2 hours ago
        f"&end_timestamp={now_ms}"
    )
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as response:
                if response.status == 200:
                    data = await response.json()
                    # Response format: {"result": {"data": [[timestamp, open, high, low, close], ...], ...}}
                    result = data.get("result", {})
                    candles = result.get("data", [])
                    if candles:
                        # Get the most recent candle's close value (index 4)
                        latest = candles[-1]
                        dvol = float(latest[4])
                        logger.info(f"Fetched live DVOL: {dvol:.1f}%")
                        return dvol
                else:
                    logger.error(f"Failed to fetch DVOL from Deribit. Status: {response.status}")
    except Exception as e:
        logger.error(f"Exception while connecting to Deribit: {e}")
    
    logger.warning(f"Using fallback IV: {DEFAULT_IV}%")
    return DEFAULT_IV

async def get_btc_price() -> float:
    """Fetch live BTC Spot price from Deribit for exact calculation consistency."""
    url = "https://deribit.com/api/v2/public/get_index_price?index_name=btc_usd"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as response:
                if response.status == 200:
                    data = await response.json()
                    if "result" in data and "index_price" in data["result"]:
                        return float(data["result"]["index_price"])
    except Exception as e:
        logger.error(f"Exception while fetching BTC index: {e}")
        
    return 0.0
