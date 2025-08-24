#!/usr/bin/env python3
"""
Energy Prices data processor for Black-Scholes model integration.
Processes comprehensive energy prices JSON data from PSE API for aFRR capacity valuation.

Usage:
    python process_energy_prices.py [--input energy_prices.json] [--output-dir pse_processed_full]
"""

import argparse
import json
import pandas as pd
from pathlib import Path
from datetime import datetime, timezone


def load_energy_prices_data(file_path: str) -> pd.DataFrame:
    """Load comprehensive energy prices JSON data."""
    if not Path(file_path).exists():
        print(f"Error: File not found at {file_path}")
        return pd.DataFrame()
    
    with open(file_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    return pd.DataFrame(data)


def process_afrr_prices_from_energy_data(df: pd.DataFrame) -> pd.DataFrame:
    """
    Extract aFRR-down prices from comprehensive energy data.
    Use CEN cost as proxy for aFRR-down capacity prices.
    """
    if df.empty:
        return df
    
    # Use dtime_utc which is already in UTC format
    result = df[['dtime_utc', 'cen_cost']].copy()
    result = result.rename(columns={
        'dtime_utc': 'dt',
        'cen_cost': 'price_pln_per_mw_h'
    })
    
    # Convert to datetime and set timezone
    result['dt'] = pd.to_datetime(result['dt']).dt.tz_localize('UTC')
    
    # Clean price data
    result['price_pln_per_mw_h'] = pd.to_numeric(result['price_pln_per_mw_h'], errors='coerce')
    result = result.dropna(subset=['price_pln_per_mw_h'])
    
    # Remove negative prices (not meaningful for capacity pricing)
    result = result[result['price_pln_per_mw_h'] > 0]
    
    return result.sort_values('dt').reset_index(drop=True)


def create_synthetic_volumes(df: pd.DataFrame) -> pd.DataFrame:
    """
    Create synthetic volume data since actual aFRR-down procurement volumes
    are not available in the energy-prices endpoint.
    Use typical market patterns based on pricing signals.
    """
    if df.empty:
        return df
    
    # Use the same datetime index as prices
    result = df[['dt']].copy()
    
    # Create synthetic procurement volumes based on typical market patterns
    # Higher prices typically correlate with higher procurement needs
    base_volume = 500  # Base procurement in MW
    price_factor = df['price_pln_per_mw_h'] / df['price_pln_per_mw_h'].median()
    
    # Add some realistic variation
    import numpy as np
    np.random.seed(42)  # For reproducible results
    noise = np.random.normal(1.0, 0.1, len(df))
    
    result['mw_procured'] = (base_volume * price_factor * noise).clip(200, 800)
    result['mw_procured'] = result['mw_procured'].round(1)
    
    return result


def process_predictors_from_energy_data(df: pd.DataFrame) -> pd.DataFrame:
    """Process predictor variables from comprehensive energy data."""
    if df.empty:
        return df
    
    predictor_cols = ['dtime_utc']
    col_mapping = {'dtime_utc': 'dt'}
    
    # Map available predictor columns
    available_predictors = {
        'cen_cost': 'CEN',
        'cor_cost': 'COR', 
        'csdac_pln': 'CSDAC',
        'ceb_sr_cost': 'CEB_SR',
        'sk_cost': 'SK'
    }
    
    for col, new_name in available_predictors.items():
        if col in df.columns:
            predictor_cols.append(col)
            col_mapping[col] = new_name
    
    result = df[predictor_cols].copy()
    result = result.rename(columns=col_mapping)
    
    # Convert datetime
    result['dt'] = pd.to_datetime(result['dt']).dt.tz_localize('UTC')
    
    # Clean numeric columns
    for col in result.columns:
        if col != 'dt':
            result[col] = pd.to_numeric(result[col], errors='coerce')
    
    return result.sort_values('dt').reset_index(drop=True)


def main():
    parser = argparse.ArgumentParser(description='Process PSE energy prices data for Black-Scholes model')
    parser.add_argument('--input', default='pse_data_js/energy_prices.json', help='Input energy prices JSON file')
    parser.add_argument('--output-dir', default='pse_processed_full', help='Output directory for processed files')
    
    args = parser.parse_args()
    
    input_file = Path(args.input)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(exist_ok=True, parents=True)
    
    print(f"Processing energy prices data from {input_file} to {output_dir}")
    
    # Load comprehensive energy prices data
    print("Loading energy prices data...")
    energy_df = load_energy_prices_data(str(input_file))
    
    if energy_df.empty:
        print("Error: No data loaded")
        return
    
    print(f"Loaded {len(energy_df)} records spanning {energy_df['dtime_utc'].min()} to {energy_df['dtime_utc'].max()}")
    
    # Process aFRR prices
    print("Processing aFRR capacity prices...")
    prices_df = process_afrr_prices_from_energy_data(energy_df)
    if not prices_df.empty:
        prices_file = output_dir / 'afrr_prices.csv'
        prices_df.to_csv(prices_file, index=False)
        print(f"Saved {len(prices_df)} price records to {prices_file}")
        print(f"Price range: {prices_df['price_pln_per_mw_h'].min():.2f} - {prices_df['price_pln_per_mw_h'].max():.2f} PLN/MW-h")
    else:
        print("Warning: No price data processed")
    
    # Create synthetic volumes (since not available in energy-prices endpoint)
    print("Creating synthetic aFRR volume data...")
    volumes_df = create_synthetic_volumes(prices_df)
    if not volumes_df.empty:
        volumes_file = output_dir / 'afrr_volumes.csv'
        volumes_df.to_csv(volumes_file, index=False)
        print(f"Saved {len(volumes_df)} synthetic volume records to {volumes_file}")
        print(f"Volume range: {volumes_df['mw_procured'].min():.1f} - {volumes_df['mw_procured'].max():.1f} MW")
    else:
        print("Warning: No volume data created")
    
    # Process predictors
    print("Processing predictor variables...")
    predictors_df = process_predictors_from_energy_data(energy_df)
    if not predictors_df.empty:
        predictors_file = output_dir / 'predictors.csv'
        predictors_df.to_csv(predictors_file, index=False)
        print(f"Saved {len(predictors_df)} predictor records to {predictors_file}")
        print(f"Predictors: {[col for col in predictors_df.columns if col != 'dt']}")
    else:
        print("Warning: No predictor data processed")
    
    # Create example command file
    example_file = output_dir / 'run_example.sh'
    predictor_cols = [col for col in predictors_df.columns if col != 'dt']
    pred_cols_str = ','.join(predictor_cols)
    
    with open(example_file, 'w') as f:
        f.write(f"""#!/usr/bin/env bash
# Example command to run Black-Scholes model with comprehensive PSE energy data
# Full year analysis: July 2024 - June 2025

python3 ../black-scholes_refactored.py \\
    --prices afrr_prices.csv --prices-dt-col dt --prices-col price_pln_per_mw_h --prices-unit per_mw_h \\
    --vols afrr_volumes.csv --vols-dt-col dt --vols-mw-col mw_procured \\
    --predictors predictors.csv --pred-dt-col dt \\
    --pred-cols {pred_cols_str} \\
    --K 400 --portfolio-mw 50 --ridge 1.0 --mode both --out-prefix energy_full_year_K400
""")
    
    example_file.chmod(0o755)
    print(f"Created example command script: {example_file}")
    
    print("\nProcessing complete!")
    print(f"Dataset summary:")
    print(f"- Time period: {energy_df['dtime_utc'].min()} to {energy_df['dtime_utc'].max()}")
    print(f"- Total intervals: {len(energy_df)}")
    print(f"- Price records: {len(prices_df)}")
    print(f"- Ready for Black-Scholes analysis with K=400 PLN/MW-h")


if __name__ == "__main__":
    main()