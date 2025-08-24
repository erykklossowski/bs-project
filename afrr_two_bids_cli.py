#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
aFRR-Down Two-Bid Command Line Interface

Comprehensive CLI for the two-bid aFRR-down valuation as specified.

Example usage:
python afrr_two_bids_cli.py \
  --cap cap.csv --cap-dt dt --cap-col afrr_d --cap-unit per_mw_h \
  --vol vol.csv --vol-dt dt --vol-mw zmb_afrrd \
  --pred pred.csv --pred-dt dt --pred-cols CEN,COR,CSDAC,SK --ridge 1.0 --cap-model parametric \
  --portfolio-mw 500 \
  --Kcap 250 \
  --Keng -50 --energy energy.csv --energy-dt dt --energy-col price_pln_per_MWh --energy-pay-rule difference --theta 0.01 \
  --out run1
"""

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional, Tuple

import pandas as pd
import numpy as np

from afrr_two_bids import value_afrr_down_two_bids
from valuation_core import read_any


def parse_capacity_bands(bands_str: str) -> List[Tuple[float, float]]:
    """
    Parse capacity banded bids from string format: "v1:K1,v2:K2,..."
    Returns list of (fraction, K_cap) tuples.
    """
    if not bands_str.strip():
        return []
    
    bands = []
    for band in bands_str.split(','):
        band = band.strip()
        if ':' not in band:
            raise ValueError(f"Invalid capacity band format: '{band}'. Expected 'fraction:K_cap'")
        
        fraction_str, kcap_str = band.split(':', 1)
        try:
            fraction = float(fraction_str)
            k_cap = float(kcap_str)
            bands.append((fraction, k_cap))
        except ValueError as e:
            raise ValueError(f"Invalid capacity band format: '{band}'. {e}")
    
    return bands


def parse_pred_cols(pred_cols_str: str) -> List[str]:
    """Parse predictor columns from comma-separated string."""
    if not pred_cols_str.strip():
        return []
    
    return [col.strip() for col in pred_cols_str.split(',')]


def save_two_bid_outputs(results: dict, out_prefix: str) -> dict:
    """
    Save two-bid analysis outputs to files.
    
    Returns dictionary of saved file paths.
    """
    saved_files = {}
    
    try:
        # 1) Summary JSON with all metrics
        summary_file = f"{out_prefix}_summary.json"
        summary_data = {k: v for k, v in results.items() if k not in ["intervals_df", "chart_data"]}
        
        # Clean data for JSON serialization
        def clean_for_json(obj):
            if hasattr(obj, '__dict__'):
                return obj.__dict__
            elif isinstance(obj, (list, tuple)):
                return [clean_for_json(item) for item in obj]
            elif isinstance(obj, dict):
                return {k: clean_for_json(v) for k, v in obj.items()}
            elif isinstance(obj, np.ndarray):
                return obj.tolist()
            else:
                return obj
        
        summary_data_clean = clean_for_json(summary_data)
        
        with open(summary_file, 'w') as f:
            json.dump(summary_data_clean, f, indent=2, default=str)
        saved_files["summary"] = summary_file
        print(f"Saved summary to: {summary_file}")
        
        # 2) Intervals CSV with detailed breakdown
        if "intervals_df" in results:
            intervals_file = f"{out_prefix}_intervals.csv"
            intervals_df = results["intervals_df"]
            
            # Select key columns for output
            output_cols = ['dt', 'price_pln_per_mw_h', 'mw_procured', 'mw_available']
            
            # Add capacity columns
            capacity_cols = [col for col in intervals_df.columns if col.startswith('p_cap_') or col.startswith('cap_revenue_')]
            output_cols.extend(capacity_cols)
            
            # Add main result columns
            main_cols = ['capacity_revenue_baseline', 'budget_envelope', 'capacity_revenue_capped', 'bound_flag']
            output_cols.extend([col for col in main_cols if col in intervals_df.columns])
            
            # Add energy columns if available
            energy_cols = [col for col in intervals_df.columns if 'energy' in col.lower() or col in ['p_energy', 'payoff_per_MWh']]
            output_cols.extend(energy_cols)
            
            # Add total revenue
            if 'total_revenue' in intervals_df.columns:
                output_cols.append('total_revenue')
            
            # Export selected columns
            available_cols = [col for col in output_cols if col in intervals_df.columns]
            intervals_df[available_cols].to_csv(intervals_file, index=False)
            saved_files["intervals"] = intervals_file
            print(f"Saved intervals data to: {intervals_file}")
        
        # 3) Daily aggregates (if enough data)
        if "intervals_df" in results:
            intervals_df = results["intervals_df"]
            
            if len(intervals_df) > 24:  # More than 6 hours of data
                intervals_df_copy = intervals_df.copy()
                intervals_df_copy['dt'] = pd.to_datetime(intervals_df_copy['dt'])
                intervals_df_copy['date'] = intervals_df_copy['dt'].dt.date
                
                # Aggregate by day
                agg_dict = {
                    'price_pln_per_mw_h': ['mean', 'min', 'max'],
                    'mw_procured': 'mean',
                    'capacity_revenue_baseline': 'sum',
                    'capacity_revenue_capped': 'sum',
                    'budget_envelope': 'sum',
                    'bound_flag': 'sum'
                }
                
                # Add energy aggregation if available
                if 'energy_revenue' in intervals_df_copy.columns:
                    agg_dict['energy_revenue'] = 'sum'
                if 'total_revenue' in intervals_df_copy.columns:
                    agg_dict['total_revenue'] = 'sum'
                
                daily_agg = intervals_df_copy.groupby('date').agg(agg_dict).round(2)
                daily_agg.columns = ['_'.join(col).strip() for col in daily_agg.columns]
                
                daily_file = f"{out_prefix}_daily.csv"
                daily_agg.to_csv(daily_file)
                saved_files["daily"] = daily_file
                print(f"Saved daily aggregates to: {daily_file}")
    
    except Exception as e:
        print(f"Warning: Error saving outputs: {e}")
    
    return saved_files


def print_two_bid_summary(results: dict):
    """Print comprehensive two-bid results to console."""
    print("\n" + "=" * 60)
    print("aFRR-DOWN TWO-BID VALUATION RESULTS")
    print("=" * 60)
    
    # Configuration
    print(f"Portfolio Size: {results.get('portfolio_mw', 0):.1f} MW")
    print(f"Total Intervals: {results.get('total_intervals', 0)}")
    print(f"Capacity Model: {results.get('cap_model', 'unknown')}")
    
    # Capacity bid configuration
    if results.get('cap_bands'):
        print(f"\nCAPACITY BIDS:")
        for i, (v_i, K_cap_i) in enumerate(results['cap_bands']):
            print(f"  Band {i+1}: {v_i:.1%} at K_cap={K_cap_i:.0f} PLN/MW-h")
    
    # Energy bid configuration
    if 'energy_leg' in results:
        energy = results['energy_leg']
        print(f"\nENERGY BID:")
        print(f"  K_energy: {energy.get('K_energy', 0):.1f} PLN/MWh")
        print(f"  Pay Rule: {energy.get('energy_pay_rule', 'unknown')}")
        print(f"  Activation Rate θ: {energy.get('theta', 0):.1%}")
    
    # CAPACITY LEG RESULTS
    print(f"\nCAPACITY LEG RESULTS:")
    print(f"  Per-MW Baseline:    {results.get('capacity_pln_perMW_baseline', 0):,.2f} PLN/MW")
    print(f"  Per-MW Capped:      {results.get('capacity_pln_perMW_capped', 0):,.2f} PLN/MW")
    print(f"  Portfolio Baseline: {results.get('capacity_pln_portfolio_baseline', 0):,.2f} PLN")
    print(f"  Portfolio Capped:   {results.get('capacity_pln_portfolio_capped', 0):,.2f} PLN")
    print(f"  Budget-Bound:       {results.get('capacity_bound_intervals_pct', 0):.1f}% of intervals")
    
    # Capacity acceptance analysis
    if results.get('capacity_acceptance_stats'):
        print(f"\nCAPACITY ACCEPTANCE ANALYSIS:")
        for acc_stat in results['capacity_acceptance_stats']:
            print(f"  Band {acc_stat['band']}: K_cap={acc_stat['K_cap']:,.0f}, "
                  f"p_acc={acc_stat['avg_acceptance_prob']:.3f}, "
                  f"MWh={acc_stat['expected_MWh']:,.0f}")
    
    # ENERGY LEG RESULTS
    if 'energy_leg' in results:
        energy = results['energy_leg']
        print(f"\nENERGY LEG RESULTS:")
        print(f"  Portfolio Revenue: {energy.get('energy_pln_portfolio', 0):,.2f} PLN")
        print(f"  Per-MW Revenue:    {energy.get('energy_pln_perMW', 0):,.2f} PLN/MW")
        print(f"  Avg Payoff:        {energy.get('avg_payoff_per_MWh', 0):,.2f} PLN/MWh")
    
    # TOTAL RESULTS
    print(f"\nTOTAL RESULTS (Capacity + Energy):")
    print(f"  Total Portfolio:   {results.get('total_pln_portfolio', 0):,.2f} PLN")
    
    # Model diagnostics
    if results.get('model_params'):
        model_params = results['model_params']
        print(f"\nMODEL DIAGNOSTICS:")
        if 'sigma' in model_params:
            print(f"  Capacity Volatility σ: {model_params['sigma']:.3f}")
        if 'used_cols' in model_params:
            print(f"  Predictors: {', '.join(model_params['used_cols'])}")
    
    print("=" * 60)


def create_two_bid_cli():
    """Create and configure the two-bid command line interface."""
    parser = argparse.ArgumentParser(
        description="aFRR-Down Two-Bid Valuation - Capacity premium & energy strike model",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Example usage:
  # Single capacity bid with energy leg
  python afrr_two_bids_cli.py \\
    --cap pse_processed_auto/afrr_prices.csv --cap-dt dt --cap-col price_pln_per_mw_h --cap-unit per_mw_h \\
    --vol pse_processed_auto/afrr_volumes.csv --vol-dt dt --vol-mw mw_procured \\
    --portfolio-mw 100 --Kcap 350 \\
    --Keng -25 --energy energy.csv --energy-dt dt --energy-col S_bal --energy-pay-rule difference --theta 0.01 \\
    --out two_bid_test

  # Banded capacity bids with energy
  python afrr_two_bids_cli.py \\
    --cap cap.csv --cap-dt dt --cap-col afrr_d --cap-unit per_mw_h \\
    --vol vol.csv --vol-dt dt --vol-mw zmb_afrrd \\
    --pred pred.csv --pred-dt dt --pred-cols CEN,COR,CSDAC,SK \\
    --portfolio-mw 500 --cap-bands 0.3:200,0.4:300,0.3:400 \\
    --Keng -50 --energy energy.csv --energy-dt dt --energy-col price_pln_per_MWh \\
    --out production_run
        """
    )
    
    # Required arguments: Capacity and Volume data
    required = parser.add_argument_group('required capacity & volume data')
    required.add_argument('--cap', required=True, help='Capacity price file (CSV/XLSX/JSON)')
    required.add_argument('--cap-dt', required=True, help='Capacity price datetime column')
    required.add_argument('--cap-col', required=True, help='Capacity price column')
    required.add_argument('--cap-unit', required=True, choices=['per_mw_h', 'per_mw_interval', 'auto'],
                         help='Capacity price units')
    
    required.add_argument('--vol', required=True, help='Volume data file (CSV/XLSX/JSON)')
    required.add_argument('--vol-dt', required=True, help='Volume datetime column')
    required.add_argument('--vol-mw', required=True, help='Volume MW column')
    
    # Portfolio and capacity bidding
    capacity = parser.add_argument_group('capacity bidding parameters')
    capacity.add_argument('--portfolio-mw', type=float, default=1.0, help='Portfolio size in MW')
    
    cap_bid_group = capacity.add_mutually_exclusive_group(required=True)
    cap_bid_group.add_argument('--Kcap', type=float, help='Single capacity bid level (PLN/MW-h)')
    cap_bid_group.add_argument('--cap-bands', type=str, help='Banded capacity bids: v1:K1,v2:K2,... (fractions:bids)')
    
    # Energy bidding (optional but recommended)
    energy = parser.add_argument_group('energy bidding parameters')
    energy.add_argument('--Keng', type=float, help='Energy strike bid (PLN/MWh, can be negative)')
    energy.add_argument('--energy', help='Balancing energy price file (CSV/XLSX/JSON)')
    energy.add_argument('--energy-dt', help='Energy datetime column')
    energy.add_argument('--energy-col', help='Energy price column')
    energy.add_argument('--energy-pay-rule', choices=['difference', 'pay_as_bid', 'pay_as_clear'], 
                       default='difference', help='Energy payoff calculation rule')
    energy.add_argument('--theta', type=float, default=0.01, help='Activation rate (MWh per MW-h)')
    
    # Modeling parameters
    modeling = parser.add_argument_group('capacity modeling parameters')
    modeling.add_argument('--cap-model', choices=['parametric', 'empirical'], default='parametric',
                         help='Capacity acceptance probability model')
    modeling.add_argument('--ridge', type=float, default=1.0, help='Ridge regularization parameter')
    
    # Predictors (optional)
    predictors = parser.add_argument_group('predictors (optional)')
    predictors.add_argument('--pred', help='Predictors file (CSV/XLSX/JSON)')
    predictors.add_argument('--pred-dt', help='Predictors datetime column')
    predictors.add_argument('--pred-cols', help='Predictor columns (comma-separated)')
    
    # Output control
    output = parser.add_argument_group('output control')
    output.add_argument('--out', default='afrr_two_bids', help='Output file prefix')
    output.add_argument('--no-intervals', action='store_true', help='Skip intervals output (faster)')
    
    return parser


def main():
    """Main CLI function."""
    parser = create_two_bid_cli()
    args = parser.parse_args()
    
    try:
        print("Loading capacity price data...")
        df_cap = read_any(args.cap)
        print(f"Loaded {len(df_cap)} capacity price records")
        
        print("Loading volume data...")
        df_vol = read_any(args.vol)
        print(f"Loaded {len(df_vol)} volume records")
        
        # Handle predictors
        df_pred = None
        pred_cols = None
        if args.pred and args.pred_dt and args.pred_cols:
            print("Loading predictor data...")
            df_pred = read_any(args.pred)
            pred_cols = parse_pred_cols(args.pred_cols)
            print(f"Loaded {len(df_pred)} predictor records with columns: {pred_cols}")
        
        # Handle capacity bids
        cap_bands = None
        single_K_cap = None
        
        if args.Kcap is not None:
            single_K_cap = args.Kcap
            print(f"Using single capacity bid: K_cap={single_K_cap} PLN/MW-h")
        elif args.cap_bands:
            cap_bands = parse_capacity_bands(args.cap_bands)
            print(f"Using banded capacity bids: {cap_bands}")
        
        # Handle energy leg
        df_energy = None
        single_K_energy = None
        
        if args.Keng is not None and args.energy:
            print("Loading energy leg data...")
            df_energy = read_any(args.energy)
            single_K_energy = args.Keng
            print(f"Loaded {len(df_energy)} energy records")
            print(f"Using energy bid: K_energy={single_K_energy} PLN/MWh")
            print(f"Energy pay rule: {args.energy_pay_rule}")
            print(f"Activation rate θ: {args.theta:.1%}")
        elif args.Keng is not None:
            print("Warning: K_energy specified but no energy data file provided. Energy leg will be skipped.")
        
        # Run two-bid valuation
        print("\nRunning aFRR-down two-bid valuation...")
        results = value_afrr_down_two_bids(
            # Capacity data
            df_cap=df_cap, dt_cap=args.cap_dt, col_cap=args.cap_col, cap_unit=args.cap_unit,
            df_vol=df_vol, dt_vol=args.vol_dt, col_vol_mw=args.vol_mw,
            
            # Predictors
            df_pred=df_pred, dt_pred=args.pred_dt, pred_cols=pred_cols,
            ridge_lambda=args.ridge, cap_model=args.cap_model,
            
            # Capacity bids
            portfolio_mw=args.portfolio_mw, cap_bands=cap_bands, single_K_cap=single_K_cap,
            
            # Energy bid
            df_energy=df_energy, dt_eng=args.energy_dt, col_energy=args.energy_col,
            single_K_energy=single_K_energy, energy_pay_rule=args.energy_pay_rule,
            theta=args.theta,
            
            # Output control
            return_intervals=not args.no_intervals
        )
        
        # Print summary
        print_two_bid_summary(results)
        
        # Save outputs
        print(f"\nSaving outputs with prefix: {args.out}")
        saved_files = save_two_bid_outputs(results, args.out)
        
        if saved_files:
            print(f"\nFiles created:")
            for file_type, file_path in saved_files.items():
                print(f"  {file_type}: {file_path}")
        
        print("\naFRR-down two-bid valuation completed successfully!")
        
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()