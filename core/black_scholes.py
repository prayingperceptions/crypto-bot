import numpy as np
from scipy.stats import norm

def calculate_probability_above_strike(
    current_price: float, 
    strike: float, 
    days_to_expiry: float, 
    implied_vol_annual: float, 
    risk_free_rate: float = 0.05
) -> float:
    """
    Calculate the theoretical probability that an asset's price will be above the
    strike price at expiry, based on the Black-Scholes model.
    
    :param current_price: Live spot price of the asset (e.g., BTC)
    :param strike: Target strike price (e.g., $100,000)
    :param days_to_expiry: Number of days remaining until contract settlement
    :param implied_vol_annual: Annualized implied volatility (e.g. 50 meaning 50%)
    :param risk_free_rate: Annual risk-free interest rate (default 5%)
    :return: Probability float [0, 1]
    """
    if days_to_expiry <= 0 or current_price <= 0 or implied_vol_annual <= 0:
        return 0.0
        
    # Convert time to years
    t_years = days_to_expiry / 365.0
    
    # Convert IV to ratio
    sigma = implied_vol_annual / 100.0
    
    # Calculate d2 (the probability that option finishes in the money)
    d1 = (np.log(current_price / strike) + (risk_free_rate + 0.5 * sigma**2) * t_years) / (sigma * np.sqrt(t_years))
    d2 = d1 - sigma * np.sqrt(t_years)
    
    # Cumulative Distribution Function of d2 gives the risk-neutral probability
    probability = float(norm.cdf(d2))
    
    return probability
