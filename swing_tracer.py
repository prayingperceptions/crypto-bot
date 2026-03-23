import asyncio
from dotenv import load_dotenv
from core.logger import setup_logger
from core.deribit import get_btc_dvol, get_btc_price
from core.black_scholes import calculate_probability_above_strike
from core.kalshi_client import KalshiClient

logger = setup_logger("swing_tracer")

async def run_tracer_cycle():
    logger.info("Starting Swing Tracer evaluation cycle...")
    
    # 1. Fetch live metrics from Deribit
    dvol = await get_btc_dvol()
    spot = await get_btc_price()
    
    if int(dvol) == 0 or int(spot) == 0:
        logger.error("Failed to fetch accurate DVOL or Spot. Skipping cycle.")
        return
        
    logger.info(f"Deribit Spot: ${spot:.2f} | DVOL: {dvol}%")
    
    # 2. Kalshi REST API integration
    # (In production, dynamic lookup of the weekly/daily markets based on series ticker)
    kalshi = KalshiClient(is_demo=True)
    # mock_market_fetch = await kalshi.get_markets(limit=50, series_ticker="KXBTCUSD")
    # For now, we mock the contract details for demonstration:
    strike = 105000.0
    days_to_expiry = 3.5  # Friday settlement
    
    # 3. Calculate BS Probability
    fair_value_prob = calculate_probability_above_strike(
        current_price=spot,
        strike=strike,
        days_to_expiry=days_to_expiry,
        implied_vol_annual=dvol
    )
    
    # 4. Compare with Kalshi
    fair_value_cents = int(fair_value_prob * 100)
    logger.info(f"Model implies Fair Value for {strike} at {days_to_expiry} days: {fair_value_cents}c")
    
    # TODO: Fetch Kalshi `mock_market` bid/ask. 
    # If fair_value_cents > (kalshi_ask_cents + EDGE_THRESHOLD), fire buy!
    await kalshi.close()

async def main():
    load_dotenv()
    logger.info("Initializing Swing Tracer (Daily/Weekly Markets).")
    
    try:
        while True:
            await run_tracer_cycle()
            # Sleep for an hour before polling again (simulating cron)
            await asyncio.sleep(3600)
    except KeyboardInterrupt:
        logger.info("Shutting down tracer.")

if __name__ == "__main__":
    asyncio.run(main())
