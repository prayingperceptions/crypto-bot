import aiohttp
import logging

logger = logging.getLogger(__name__)

async def get_btc_dvol() -> float:
    """
    Fetch the live Bitcoin Implied Volatility (DVOL) from Deribit.
    Returns the annualized implied volatility (e.g., 55.4 means 55.4%).
    """
    url = "https://www.deribit.com/api/v2/public/get_index_price?index_name=btc_dvol"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    if "result" in data and "index_price" in data["result"]:
                        return float(data["result"]["index_price"])
                else:
                    logger.error(f"Failed to fetch DVOL from Deribit. Status: {response.status}")
    except Exception as e:
        logger.error(f"Exception while connecting to Deribit: {e}")
        
    return 0.0

async def get_btc_price() -> float:
    """Fetch live BTC Spot price from Deribit for exact calculation consistency."""
    url = "https://www.deribit.com/api/v2/public/get_index_price?index_name=btc_usd"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    if "result" in data and "index_price" in data["result"]:
                        return float(data["result"]["index_price"])
    except Exception as e:
        logger.error(f"Exception while fetching BTC index: {e}")
        
    return 0.0
