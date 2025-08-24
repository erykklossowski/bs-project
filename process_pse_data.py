#!/usr/bin/env python3
"""
PSE data processor for Black-Scholes model integration.
Converts raw PSE JSON files to structured data files suitable for black-scholes_refactored.py

Usage:
    python process_pse_data.py [--raw-dir pse_raw] [--output-dir pse_processed]
"""

import argparse
import json
import pandas as pd
from pathlib import Path
from datetime import datetime, timezone
import glob


def load_paginated_json(raw_dir: str, prefix: str) -> pd.DataFrame:
    """Load and combine all paginated JSON files for a given prefix."""
    pattern = f"{raw_dir}/{prefix}_p*.json"
    files = sorted(glob.glob(pattern))
    
    if not files:
        print(f"Warning: No paginated files found for pattern {pattern}")
        # Try single file fallback
        single_file = f"{raw_dir}/{prefix}.json"
        if Path(single_file).exists():
            with open(single_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, list):
                return pd.DataFrame(data)
            elif isinstance(data, dict) and 'value' in data:
                return pd.DataFrame(data['value'])
        return pd.DataFrame()
    
    all_data = []
    for file_path in files:
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        # Handle both array and {value: []} formats
        if isinstance(data, list):
            all_data.extend(data)
        elif isinstance(data, dict) and 'value' in data:
            all_data.extend(data['value'])
        else:
            print(f"Warning: Unexpected format in {file_path}")
    
    return pd.DataFrame(all_data)


def create_datetime_column(df: pd.DataFrame) -> pd.DataFrame:
    """Create UTC datetime column from dtime_utc."""
    if df.empty:
        return df
    
    df = df.copy()
    # Use the dtime_utc column which is already in UTC
    df['dt'] = pd.to_datetime(df['dtime_utc'])
    df['dt'] = df['dt'].dt.tz_localize('UTC')
    
    return df


def process_afrr_prices(df: pd.DataFrame) -> pd.DataFrame:
    """
    Process aFRR-down capacity prices from cmbu-tu data.
    
    CRITICAL: Uses afrr_d as the PRIMARY underlying price for Black-Scholes option pricing.
    This is the actual aFRR-down capacity marginal price, not a proxy.
    
    Units: Converts to PLN/MW-h (standard capacity pricing units)
    Granularity: 15-minute intervals aligned to market operations
    """
    if df.empty:
        return df
    
    df = create_datetime_column(df)
    
    # UNDERLYING PRICE: afrr_d is the actual aFRR-down capacity marginal price
    # This is used as S(t) in the Black-Scholes formula E[(S-K)^+]
    result = df[['dt', 'afrr_d']].copy()
    result = result.rename(columns={'afrr_d': 'price_pln_per_mw_h'})
    
    # Clean and validate pricing data
    result['price_pln_per_mw_h'] = pd.to_numeric(result['price_pln_per_mw_h'], errors='coerce')
    result = result.dropna(subset=['price_pln_per_mw_h'])
    
    # Remove negative/zero prices (not economically meaningful for capacity)
    result = result[result['price_pln_per_mw_h'] > 0]
    
    return result.sort_values('dt').reset_index(drop=True)


def process_afrr_volumes(df: pd.DataFrame) -> pd.DataFrame:
    """
    Process aFRR-down procurement volumes from zmb data.
    
    CRITICAL: Uses zmb_afrrd as the ACTUAL procured MW for budget constraints.
    This is real market data, not synthetic volumes.
    
    Budget Formula: price_afrrd × mw_procured_afrrd × Δh per interval
    Portfolio Cap: portfolio_revenues ≤ fair_share_of_interval_budget
    """
    if df.empty:
        return df
    
    df = create_datetime_column(df)
    
    # ACTUAL PROCUREMENT: zmb_afrrd is the real aFRR-down MW procured by PSE
    # This is used for budget constraint calculations: price × MW × hours
    result = df[['dt', 'zmb_afrrd']].copy()
    result = result.rename(columns={'zmb_afrrd': 'mw_procured'})
    
    # Clean and validate volume data
    result['mw_procured'] = pd.to_numeric(result['mw_procured'], errors='coerce')
    result = result.dropna(subset=['mw_procured'])
    
    # Remove negative procurement (not economically meaningful)
    result = result[result['mw_procured'] >= 0]
    
    return result.sort_values('dt').reset_index(drop=True)


def process_predictors(price_cost_df: pd.DataFrame, csdac_df: pd.DataFrame) -> pd.DataFrame:
    """
    Process predictor variables for μₜ drift modeling in Black-Scholes.
    
    IMPORTANT: These are PREDICTORS ONLY, not the underlying price.
    - CEN, COR, SK: Market pricing mechanisms that influence aFRR-down price expectations
    - CSDAC: Day-ahead market price for reference
    
    Usage: Fed into design_matrix() → fit_ridge() → μₜ estimation for option pricing
    NOT used as the underlying price S(t) - that's afrr_d from process_afrr_prices()
    """
    predictors = pd.DataFrame()
    
    # Process market predictors (CEN, COR, SK are predictors for μₜ, NOT underlying)
    if not price_cost_df.empty:
        pc_df = create_datetime_column(price_cost_df)
        
        # PREDICTORS: Market variables that help predict aFRR-down price movements
        available_cols = ['dt']
        col_mapping = {}
        
        if 'cen_cost' in pc_df.columns:
            available_cols.append('cen_cost')
            col_mapping['cen_cost'] = 'CEN'  # Central energy market predictor
            
        if 'cor_pp_cost' in pc_df.columns:
            available_cols.append('cor_pp_cost')
            col_mapping['cor_pp_cost'] = 'COR'  # Corrective pricing predictor
            
        if 'sk_cost' in pc_df.columns:
            available_cols.append('sk_cost')
            col_mapping['sk_cost'] = 'SK'  # System cost predictor
            
        if 'ceb_sr_cost' in pc_df.columns:
            available_cols.append('ceb_sr_cost')
            col_mapping['ceb_sr_cost'] = 'CEB_SR'  # Balancing energy SR predictor
            
        predictors_pc = pc_df[available_cols].copy()
        predictors_pc = predictors_pc.rename(columns=col_mapping)
        predictors = predictors_pc
    
    # Process CSDAC data as additional predictor
    if not csdac_df.empty:
        csdac_processed = create_datetime_column(csdac_df)
        csdac_processed = csdac_processed[['dt', 'csdac_pln']].copy()
        csdac_processed = csdac_processed.rename(columns={'csdac_pln': 'CSDAC'})  # Day-ahead price predictor
        
        if predictors.empty:
            predictors = csdac_processed
        else:
            predictors = predictors.merge(csdac_processed, on='dt', how='outer')
    
    if not predictors.empty:
        # Clean predictor data
        for col in predictors.columns:
            if col != 'dt':
                predictors[col] = pd.to_numeric(predictors[col], errors='coerce')
        
        predictors = predictors.sort_values('dt').reset_index(drop=True)
    
    return predictors


def main():
    parser = argparse.ArgumentParser(description='Process PSE raw data for Black-Scholes model')
    parser.add_argument('--raw-dir', default='pse_raw', help='Directory containing raw PSE JSON files')
    parser.add_argument('--output-dir', default='pse_processed', help='Output directory for processed files')
    
    args = parser.parse_args()
    
    raw_dir = Path(args.raw_dir)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(exist_ok=True, parents=True)
    
    print(f"Processing PSE data from {raw_dir} to {output_dir}")
    
    # Load raw data from corrected endpoints
    print("Loading raw aFRR data...")
    print("- afrr-prices: aFRR-down capacity marginal prices (afrr_d)")
    print("- afrr-volumes: aFRR-down procurement volumes (zmb_afrrd)")
    print("- market-predictors: CEN/COR/SK predictors for μₜ modeling")
    print("- sdac-prices: CSDAC predictor data")
    
    cmbu_tu_df = load_paginated_json(str(raw_dir), 'afrr-prices')
    zmb_df = load_paginated_json(str(raw_dir), 'afrr-volumes') 
    price_cost_df = load_paginated_json(str(raw_dir), 'market-predictors')
    csdac_df = load_paginated_json(str(raw_dir), 'sdac-prices')
    
    # Process aFRR-down capacity prices (PRIMARY UNDERLYING)
    print("\n=== Processing aFRR-down capacity prices (PRIMARY UNDERLYING) ===")
    print("Using afrr_d field as S(t) for Black-Scholes option pricing")
    prices_df = process_afrr_prices(cmbu_tu_df)
    if not prices_df.empty:
        prices_file = output_dir / 'afrr_prices.csv'
        prices_df.to_csv(prices_file, index=False)
        print(f"✓ Saved {len(prices_df)} aFRR-down price records to {prices_file}")
        print(f"  Price range: {prices_df['price_pln_per_mw_h'].min():.2f} - {prices_df['price_pln_per_mw_h'].max():.2f} PLN/MW-h")
    else:
        print("ERROR: No aFRR-down price data processed - cannot proceed with analysis")
        return
    
    # Process aFRR-down procurement volumes (ACTUAL VOLUMES)
    print("\n=== Processing aFRR-down procurement volumes (ACTUAL VOLUMES) ===")
    print("Using zmb_afrrd field for real budget constraints: price × MW × hours")
    volumes_df = process_afrr_volumes(zmb_df)
    if not volumes_df.empty:
        volumes_file = output_dir / 'afrr_volumes.csv'
        volumes_df.to_csv(volumes_file, index=False)
        print(f"✓ Saved {len(volumes_df)} actual volume records to {volumes_file}")
        print(f"  Volume range: {volumes_df['mw_procured'].min():.1f} - {volumes_df['mw_procured'].max():.1f} MW")
    else:
        print("ERROR: No aFRR-down volume data processed - cannot compute budget constraints")
        return
    
    # Process market predictors (FOR μₜ MODELING ONLY)
    print("\n=== Processing market predictors (FOR μₜ DRIFT MODELING ONLY) ===")
    print("CEN, COR, SK, CSDAC as predictors for time-varying drift μₜ - NOT underlying price")
    predictors_df = process_predictors(price_cost_df, csdac_df)
    if not predictors_df.empty:
        predictors_file = output_dir / 'predictors.csv'
        predictors_df.to_csv(predictors_file, index=False)
        available_predictors = [col for col in predictors_df.columns if col != 'dt']
        print(f"✓ Saved {len(predictors_df)} predictor records to {predictors_file}")
        print(f"  Available predictors: {available_predictors}")
    else:
        print("Warning: No predictor data processed - will use constant μ for drift modeling")
    
    # Create example command file with corrected architecture
    example_file = output_dir / 'run_example.sh'
    available_predictors = [col for col in predictors_df.columns if col != 'dt'] if not predictors_df.empty else []
    pred_cols_str = ','.join(available_predictors) if available_predictors else "CEN,COR,SK"
    
    with open(example_file, 'w') as f:
        f.write(f"""#!/usr/bin/env bash
# Black-Scholes aFRR-down valuation with CORRECTED architecture
#
# UNDERLYING PRICE: afrr_d (actual aFRR-down capacity marginal price)
# PROCUREMENT VOLUMES: zmb_afrrd (actual procured MW for budget constraints)  
# PREDICTORS: CEN,COR,SK,CSDAC (for μₜ drift modeling ONLY)
#
# Budget formula: price_afrrd × mw_procured_afrrd × Δh per interval
# Portfolio cap: revenues ≤ fair_share × interval_budget

python3 ../black-scholes_refactored.py \\
    --prices afrr_prices.csv --prices-dt-col dt --prices-col price_pln_per_mw_h --prices-unit per_mw_h \\
    --vols afrr_volumes.csv --vols-dt-col dt --vols-mw-col mw_procured \\
    --predictors predictors.csv --pred-dt-col dt \\
    --pred-cols {pred_cols_str} \\
    --K 400 --portfolio-mw 50 --ridge 1.0 --mode both --out-prefix corrected_afrr_K400

# Key corrections from previous version:
# - Underlying price: afrr_d (not CEN cost proxy)
# - Volumes: zmb_afrrd actual data (not synthetic)  
# - Predictors: CEN/COR/SK for μₜ only (not underlying)
# - Budget: explicit price × MW × hours formula
""")
    
    example_file.chmod(0o755)
    print(f"\n✓ Created corrected example command script: {example_file}")
    
    print(f"\n{'='*60}")
    print("PSE DATA PROCESSING COMPLETE - CORRECTED ARCHITECTURE")
    print(f"{'='*60}")
    print(f"✓ aFRR-down prices (afrr_d): {len(prices_df)} records")
    print(f"✓ aFRR-down volumes (zmb_afrrd): {len(volumes_df)} records")
    print(f"✓ Market predictors: {len(predictors_df)} records")
    print(f"\nData architecture:")
    print(f"- UNDERLYING: afrr_d → price_pln_per_mw_h → S(t) in E[(S-K)^+]")
    print(f"- VOLUMES: zmb_afrrd → mw_procured → budget = price × MW × 0.25h")
    print(f"- PREDICTORS: {available_predictors} → μₜ drift modeling")
    print(f"\nReady for Black-Scholes analysis with corrected data sources!")


if __name__ == "__main__":
    main()