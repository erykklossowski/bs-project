#!/usr/bin/env python3
"""
Create synthetic balancing energy price data for testing the two-bid system.
"""

import pandas as pd
import numpy as np

def create_synthetic_energy_data():
    """Create synthetic balancing energy prices with realistic patterns."""
    
    # Create same time range as PSE data
    dates = pd.date_range("2024-06-30 22:00:00", "2024-07-11 02:00:00", freq="15min", tz="UTC")
    
    # Create synthetic balancing energy prices (can be negative)
    np.random.seed(42)  # For reproducible results
    
    # Base pattern with daily and weekly cycles
    hours = np.array([dt.hour for dt in dates])
    days = np.array([dt.dayofweek for dt in dates])
    
    # Price patterns
    base_price = 80.0  # Base balancing energy price
    
    # Daily pattern (higher during peak hours)
    daily_pattern = 20.0 * np.sin(2 * np.pi * hours / 24) + 10.0 * np.sin(4 * np.pi * hours / 24)
    
    # Weekly pattern (higher on weekdays)
    weekly_pattern = 15.0 * (days < 5) - 10.0 * (days >= 5)
    
    # Random variations
    noise = np.random.normal(0, 25, len(dates))
    
    # Occasional negative prices (surplus situations)
    negative_spikes = np.random.choice([0, -100, -150], size=len(dates), p=[0.95, 0.03, 0.02])
    
    # Combine all patterns
    S_bal = base_price + daily_pattern + weekly_pattern + noise + negative_spikes
    
    # Create DataFrame
    energy_df = pd.DataFrame({
        'dt': dates,
        'S_bal': S_bal.round(2)
    })
    
    # Save to CSV
    energy_df.to_csv('synthetic_energy_prices.csv', index=False)
    
    print(f"Created synthetic energy data: {len(energy_df)} records")
    print(f"Price range: {S_bal.min():.1f} to {S_bal.max():.1f} PLN/MWh")
    print(f"Negative prices: {(S_bal < 0).sum()} intervals ({100*(S_bal < 0).mean():.1f}%)")
    
    return energy_df

if __name__ == "__main__":
    create_synthetic_energy_data()