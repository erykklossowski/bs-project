#!/usr/bin/env python3
"""
Quick test to debug the web UI issue.
"""

from afrr_valuation import value_afrr_down
import pandas as pd
import json
from pathlib import Path

def test_web_ui_issue():
    """Test the exact same call as web UI makes."""
    
    # Load PSE data
    prices_df = pd.read_csv("pse_processed_auto/afrr_prices.csv").head(50)
    vols_df = pd.read_csv("pse_processed_auto/afrr_volumes.csv").head(50)
    
    print("Running aFRR valuation...")
    
    try:
        results = value_afrr_down(
            # Capacity price & volumes
            df_cap=prices_df, dt_col_cap="dt", price_col_cap="price_pln_per_mw_h", price_unit="per_mw_h",
            df_vol=vols_df, dt_col_vol="dt", mw_col_vol="mw_procured",
            
            # Predictors (none)
            df_pred=None, dt_col_pred=None, pred_cols=None,
            ridge_lambda=1.0, model_mode="parametric",
            
            # Bids
            portfolio_mw=25.0, bids=None, single_K=350.0,
            
            # Energy leg
            theta_list=None, risk_uplift=0.15,
            
            return_intervals=True
        )
        
        print("✓ aFRR valuation succeeded")
        print(f"Keys in results: {list(results.keys())}")
        
        # Try to JSON serialize each key
        for key, value in results.items():
            try:
                if key not in ["intervals_df", "chart_data"]:
                    json.dumps(value, default=str)
                    print(f"✓ {key}: JSON serializable")
                else:
                    print(f"◦ {key}: Skipped (DataFrame/chart data)")
            except Exception as e:
                print(f"✗ {key}: JSON error - {e}")
                print(f"  Type: {type(value)}")
                if hasattr(value, '__dict__'):
                    print(f"  Dict: {value.__dict__}")
                else:
                    print(f"  Value: {str(value)[:200]}...")
        
    except Exception as e:
        print(f"✗ aFRR valuation failed: {e}")

if __name__ == "__main__":
    test_web_ui_issue()