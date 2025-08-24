#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
aFRR-Down Command Line Interface

Comprehensive CLI for the two-leg aFRR-down valuation as specified in prompt.md.

Example usage:
python afrr_down.py \
  --cap-file cap.csv --cap-dt dt --cap-col afrr_d --cap-unit per_mw_h \
  --vol-file vol.csv --vol-dt dt --vol-col zmb_afrrd \
  --pred-file pred.csv --pred-dt dt --pred-cols CEN,COR,CSDAC,SK --ridge 1.0 \
  --portfolio-mw 500 \
  --single-K 250 \
  --model-mode parametric \
  --energy-file bal.csv --energy-dt dt --energy-col price_pln_per_MWh \
  --alt-file csdac.csv --alt-dt dt --alt-col csdac_pln \
  --theta 0.005,0.01,0.02,0.05 \
  --out-prefix run1
"""

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional, Tuple

import pandas as pd
import numpy as np

from afrr_valuation import value_afrr_down
from valuation_core import read_any


def parse_bids(bands_str: str) -> List[Tuple[float, float]]:
    """
    Parse banded bids from string format: "v1:K1,v2:K2,..."
    Returns list of (fraction, bid) tuples.
    """
    if not bands_str.strip():
        return []
    
    bids = []
    for band in bands_str.split(','):
        band = band.strip()
        if ':' not in band:
            raise ValueError(f"Invalid bid format: '{band}'. Expected 'fraction:bid'")
        
        fraction_str, bid_str = band.split(':', 1)
        try:
            fraction = float(fraction_str)
            bid = float(bid_str)
            bids.append((fraction, bid))
        except ValueError as e:
            raise ValueError(f"Invalid bid format: '{band}'. {e}")
    
    return bids


def parse_theta_list(theta_str: str) -> List[float]:
    """Parse theta list from comma-separated string."""
    if not theta_str.strip():
        return []
    
    try:
        return [float(x.strip()) for x in theta_str.split(',')]
    except ValueError as e:
        raise ValueError(f"Invalid theta format: {e}")


def save_outputs(results: dict, out_prefix: str) -> dict:
    """
    Save analysis outputs to files as specified in prompt.md.
    
    Returns dictionary of saved file paths.
    """
    saved_files = {}
    
    try:
        # 1) Intervals CSV: dt, Pcap, Qproc, p_acc(K_i), R_cap, B, R_cap_capped, bound_flag
        if "intervals_df" in results:
            intervals_file = f"{out_prefix}_intervals.csv"
            intervals_df = results["intervals_df"]
            intervals_df.to_csv(intervals_file, index=False)
            saved_files["intervals"] = intervals_file
            print(f"Saved intervals data to: {intervals_file}")
        
        # 2) Summary JSON: all headline metrics
        summary_file = f"{out_prefix}_summary.json"
        summary_data = {
            # Remove DataFrames for JSON serialization
            k: v for k, v in results.items() 
            if k not in ["intervals_df", "chart_data"]
        }
        
        with open(summary_file, 'w') as f:
            json.dump(summary_data, f, indent=2, default=str)
        saved_files["summary"] = summary_file
        print(f"Saved summary to: {summary_file}")
        
        # 3) Daily aggregates (if we have enough data)
        if "intervals_df" in results:
            intervals_df = results["intervals_df"]
            
            if len(intervals_df) > 24:  # More than 6 hours of data
                intervals_df['dt'] = pd.to_datetime(intervals_df['dt'])
                intervals_df['date'] = intervals_df['dt'].dt.date
                
                # Aggregate by day
                daily_agg = intervals_df.groupby('date').agg({
                    'price_pln_per_mw_h': ['mean', 'min', 'max'],
                    'mw_procured': 'mean',
                    'capacity_revenue_baseline': 'sum',
                    'capacity_revenue_capped': 'sum',
                    'budget_envelope': 'sum',
                    'bound_flag': 'sum'
                }).round(2)
                
                # Flatten column names
                daily_agg.columns = ['_'.join(col).strip() for col in daily_agg.columns]
                
                daily_file = f"{out_prefix}_daily.csv"
                daily_agg.to_csv(daily_file)
                saved_files["daily"] = daily_file
                print(f"Saved daily aggregates to: {daily_file}")
        
        # 4) Monthly aggregates (if we have enough data)
        if "intervals_df" in results and len(results["intervals_df"]) > 24*30:
            intervals_df = results["intervals_df"]
            intervals_df['dt'] = pd.to_datetime(intervals_df['dt'])
            intervals_df['year_month'] = intervals_df['dt'].dt.to_period('M')
            
            monthly_agg = intervals_df.groupby('year_month').agg({
                'price_pln_per_mw_h': ['mean', 'min', 'max'],
                'mw_procured': 'mean',
                'capacity_revenue_baseline': 'sum',
                'capacity_revenue_capped': 'sum',
                'budget_envelope': 'sum',
                'bound_flag': 'sum'
            }).round(2)
            
            monthly_agg.columns = ['_'.join(col).strip() for col in monthly_agg.columns]
            
            monthly_file = f"{out_prefix}_monthly.csv"
            monthly_agg.to_csv(monthly_file)
            saved_files["monthly"] = monthly_file
            print(f"Saved monthly aggregates to: {monthly_file}")
    
    except Exception as e:
        print(f"Warning: Error saving outputs: {e}")
    
    return saved_files


def print_summary(results: dict):
    """Print summary results to console."""
    print("\n" + "=" * 60)
    print("aFRR-DOWN VALUATION RESULTS")
    print("=" * 60)
    
    # Portfolio summary
    print(f"Portfolio Size: {results.get('portfolio_mw', 0):.1f} MW")
    print(f"Total Intervals: {results.get('total_intervals', 0)}")
    
    # Capacity leg results
    print(f"\nCAPACITY LEG:")
    print(f"  Portfolio Baseline: {results.get('portfolio_baseline_PLN', 0):,.2f} PLN")
    print(f"  Portfolio Capped:   {results.get('portfolio_capped_PLN', 0):,.2f} PLN")
    print(f"  Per-MW Baseline:    {results.get('capacity_perMW_baseline_PLN', 0):,.2f} PLN/MW")
    print(f"  Per-MW Capped:      {results.get('capacity_perMW_capped_PLN', 0):,.2f} PLN/MW")
    print(f"  Bound Intervals:    {results.get('bound_intervals_pct', 0):.1f}%")
    
    # Bid analysis
    if results.get('acceptance_stats'):
        print(f"\nBID ANALYSIS:")
        for i, acc_stat in enumerate(results['acceptance_stats']):
            print(f"  Band {i+1}: K={acc_stat['bid_K']:,.0f} PLN/MW-h, "
                  f"fraction={acc_stat['fraction']:.1%}, "
                  f"p_acc={acc_stat['avg_acceptance_prob']:.3f}, "
                  f"MWh={acc_stat['expected_MWh']:,.1f}")
    
    # Model diagnostics
    if results.get('model_params'):
        model_params = results['model_params']
        print(f"\nMODEL DIAGNOSTICS:")
        print(f"  Mode: {model_params.get('model_mode', 'unknown')}")
        if 'sigma' in model_params:
            print(f"  Volatility (σ): {model_params['sigma']:.3f}")
        if 'used_cols' in model_params:
            print(f"  Predictors: {', '.join(model_params['used_cols'])}")
    
    # Energy leg results
    if 'energy_leg' in results:
        energy_results = results['energy_leg']
        print(f"\nENERGY LEG (SHORT PUT):")
        print(f"  Expected Loss: {energy_results.get('energy_loss_pln_per_mwh', 0):.2f} PLN/MWh")
        
        if 'theta_sweep' in energy_results:
            print(f"  Theta Sweep (Premium Floors):")
            for theta_result in energy_results['theta_sweep']:
                print(f"    θ={theta_result['theta_pct']:4.1f}%: "
                      f"{theta_result['premium_floor_pln_per_mwh']:6.2f} PLN/MW-h "
                      f"(rec: {theta_result['premium_recommended_pln_per_mwh']:6.2f})")
    
    print("=" * 60)


def create_cli():
    """Create and configure the command line interface."""
    parser = argparse.ArgumentParser(
        description="aFRR-Down Valuation - Two-leg product model",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Example usage:
  python afrr_down.py \\
    --cap-file pse_processed_auto/afrr_prices.csv --cap-dt dt --cap-col price_pln_per_mw_h --cap-unit per_mw_h \\
    --vol-file pse_processed_auto/afrr_volumes.csv --vol-dt dt --vol-col mw_procured \\
    --portfolio-mw 100 --single-K 400 --model-mode parametric --out-prefix test_run

  python afrr_down.py \\
    --cap-file cap.csv --cap-dt dt --cap-col afrr_d --cap-unit per_mw_h \\
    --vol-file vol.csv --vol-dt dt --vol-col zmb_afrrd \\
    --pred-file pred.csv --pred-dt dt --pred-cols CEN,COR,CSDAC,SK --ridge 1.0 \\
    --portfolio-mw 500 --bands 0.3:200,0.4:300,0.3:400 \\
    --energy-file bal.csv --energy-dt dt --energy-col price_pln_per_MWh \\
    --alt-file csdac.csv --alt-dt dt --alt-col csdac_pln \\
    --theta 0.005,0.01,0.02,0.05 --out-prefix production_run
        """
    )
    
    # Required arguments: Capacity and Volume data
    required = parser.add_argument_group('required arguments')
    required.add_argument('--cap-file', required=True, help='Capacity price file (CSV/XLSX/JSON)')
    required.add_argument('--cap-dt', required=True, help='Capacity price datetime column')
    required.add_argument('--cap-col', required=True, help='Capacity price column')
    required.add_argument('--cap-unit', required=True, choices=['per_mw_h', 'per_mw_interval', 'auto'],
                         help='Capacity price units')
    
    required.add_argument('--vol-file', required=True, help='Volume data file (CSV/XLSX/JSON)')
    required.add_argument('--vol-dt', required=True, help='Volume datetime column')
    required.add_argument('--vol-col', required=True, help='Volume MW column')
    
    # Portfolio and bidding
    bidding = parser.add_argument_group('bidding parameters')
    bidding.add_argument('--portfolio-mw', type=float, default=1.0, help='Portfolio size in MW')
    
    bid_group = bidding.add_mutually_exclusive_group(required=True)
    bid_group.add_argument('--single-K', type=float, help='Single bid level (PLN/MW-h)')
    bid_group.add_argument('--bands', type=str, help='Banded bids: v1:K1,v2:K2,... (fractions:bids)')
    
    # Modeling
    modeling = parser.add_argument_group('modeling parameters')
    modeling.add_argument('--model-mode', choices=['parametric', 'empirical'], default='parametric',
                         help='Acceptance probability model')
    modeling.add_argument('--ridge', type=float, default=1.0, help='Ridge regularization parameter')
    
    # Predictors (optional)
    predictors = parser.add_argument_group('predictors (optional)')
    predictors.add_argument('--pred-file', help='Predictors file (CSV/XLSX/JSON)')
    predictors.add_argument('--pred-dt', help='Predictors datetime column')
    predictors.add_argument('--pred-cols', help='Predictor columns (comma-separated)')
    
    # Energy leg (optional)
    energy = parser.add_argument_group('energy leg (optional)')
    energy.add_argument('--energy-file', help='Balancing energy price file')
    energy.add_argument('--energy-dt', help='Energy datetime column')
    energy.add_argument('--energy-col', help='Energy price column')
    
    energy.add_argument('--alt-file', help='Alternative price file')
    energy.add_argument('--alt-dt', help='Alternative datetime column')
    energy.add_argument('--alt-col', help='Alternative price column')
    
    energy.add_argument('--theta', help='Activation intensities (comma-separated, e.g., 0.005,0.01,0.02)')
    energy.add_argument('--risk-uplift', type=float, default=0.15, help='Risk uplift factor')
    
    # Output control
    output = parser.add_argument_group('output control')
    output.add_argument('--out-prefix', default='afrr_valuation', help='Output file prefix')
    output.add_argument('--no-intervals', action='store_true', help='Skip intervals output (faster)')
    
    return parser


def main():
    """Main CLI function."""
    parser = create_cli()
    args = parser.parse_args()
    
    try:
        print("Loading capacity price data...")
        df_cap = read_any(args.cap_file)
        print(f"Loaded {len(df_cap)} capacity price records")
        
        print("Loading volume data...")
        df_vol = read_any(args.vol_file)
        print(f"Loaded {len(df_vol)} volume records")
        
        # Handle predictors
        df_pred = None
        pred_cols = None
        if args.pred_file and args.pred_dt and args.pred_cols:
            print("Loading predictor data...")
            df_pred = read_any(args.pred_file)
            pred_cols = [col.strip() for col in args.pred_cols.split(',')]
            print(f"Loaded {len(df_pred)} predictor records with columns: {pred_cols}")
        
        # Handle bids
        bids = None
        single_K = None
        
        if args.single_K is not None:
            single_K = args.single_K
            print(f"Using single bid: K={single_K} PLN/MW-h")
        elif args.bands:
            bids = parse_bids(args.bands)
            print(f"Using banded bids: {bids}")
        
        # Handle energy leg
        df_energy = None
        df_alt = None
        theta_list = None
        
        if args.energy_file and args.alt_file:
            print("Loading energy leg data...")
            df_energy = read_any(args.energy_file)
            df_alt = read_any(args.alt_file)
            print(f"Loaded {len(df_energy)} energy records and {len(df_alt)} alternative price records")
            
            if args.theta:
                theta_list = parse_theta_list(args.theta)
                print(f"Using theta values: {[f'{t:.1%}' for t in theta_list]}")
        
        # Run valuation
        print("\nRunning aFRR-down valuation...")
        results = value_afrr_down(
            # Capacity and volume
            df_cap=df_cap, dt_col_cap=args.cap_dt, price_col_cap=args.cap_col, price_unit=args.cap_unit,
            df_vol=df_vol, dt_col_vol=args.vol_dt, mw_col_vol=args.vol_col,
            
            # Predictors
            df_pred=df_pred, dt_col_pred=args.pred_dt, pred_cols=pred_cols, 
            ridge_lambda=args.ridge, model_mode=args.model_mode,
            
            # Bids
            portfolio_mw=args.portfolio_mw, bids=bids, single_K=single_K,
            
            # Energy leg
            df_energy=df_energy, dt_col_energy=args.energy_dt, energy_col=args.energy_col,
            df_alt=df_alt, dt_col_alt=args.alt_dt, alt_col=args.alt_col,
            theta_list=theta_list, risk_uplift=args.risk_uplift,
            
            # Output control
            return_intervals=not args.no_intervals
        )
        
        # Print summary
        print_summary(results)
        
        # Save outputs
        print(f"\nSaving outputs with prefix: {args.out_prefix}")
        saved_files = save_outputs(results, args.out_prefix)
        
        if saved_files:
            print(f"\nFiles created:")
            for file_type, file_path in saved_files.items():
                print(f"  {file_type}: {file_path}")
        
        print("\naFRR-down valuation completed successfully!")
        
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()