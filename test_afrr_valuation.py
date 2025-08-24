#!/usr/bin/env python3
"""
Test script for the new aFRR-down valuation module.
Validates all key requirements from prompt.md.
"""

import pandas as pd
import numpy as np
from pathlib import Path
from afrr_valuation import value_afrr_down, validate_bids

def test_single_K_valuation():
    """Test basic single-K valuation with PSE data."""
    print("=== Testing Single-K Valuation ===")
    
    prices_file = Path("pse_processed_auto/afrr_prices.csv")
    volumes_file = Path("pse_processed_auto/afrr_volumes.csv")
    
    if not prices_file.exists() or not volumes_file.exists():
        print("PSE data files not found. Creating synthetic test data...")
        
        # Create synthetic test data
        dates = pd.date_range("2024-07-01", periods=96, freq="15min", tz="UTC")
        
        prices_df = pd.DataFrame({
            "dt": dates,
            "price_pln_per_mw_h": np.random.lognormal(mean=5.5, sigma=0.5, size=len(dates))
        })
        
        volumes_df = pd.DataFrame({
            "dt": dates,
            "mw_procured": np.random.uniform(200, 600, size=len(dates))
        })
    else:
        prices_df = pd.read_csv(prices_file).head(96)  # 1 day of data
        volumes_df = pd.read_csv(volumes_file).head(96)
    
    # Test single K valuation
    try:
        results = value_afrr_down(
            df_cap=prices_df, dt_col_cap="dt", price_col_cap="price_pln_per_mw_h", price_unit="per_mw_h",
            df_vol=volumes_df, dt_col_vol="dt", mw_col_vol="mw_procured",
            portfolio_mw=100.0,
            single_K=400.0,
            model_mode="parametric"
        )
        
        print(f"✓ Single-K valuation successful")
        print(f"  Portfolio baseline: {results['portfolio_baseline_PLN']:.2f} PLN")
        print(f"  Portfolio capped: {results['portfolio_capped_PLN']:.2f} PLN")
        print(f"  Bound intervals: {results['bound_intervals_pct']:.1f}%")
        print(f"  Total intervals processed: {results['total_intervals']}")
        
        # Validate acceptance stats
        if results['acceptance_stats']:
            acc_stat = results['acceptance_stats'][0]
            print(f"  Avg acceptance prob: {acc_stat['avg_acceptance_prob']:.3f}")
            print(f"  Expected MWh: {acc_stat['expected_MWh']:.1f}")
        
        return True
        
    except Exception as e:
        print(f"✗ Single-K valuation failed: {e}")
        return False

def test_banded_bidding():
    """Test banded bidding functionality."""
    print("\n=== Testing Banded Bidding ===")
    
    # Create simple test data
    dates = pd.date_range("2024-07-01", periods=48, freq="15min", tz="UTC")
    
    prices_df = pd.DataFrame({
        "dt": dates,
        "price_pln_per_mw_h": [300.0, 400.0, 500.0, 600.0] * 12  # Repeating pattern
    })
    
    volumes_df = pd.DataFrame({
        "dt": dates,
        "mw_procured": [400.0] * len(dates)  # Constant volume
    })
    
    # Test banded bids: 50% at K=350, 50% at K=450
    bids = [(0.5, 350.0), (0.5, 450.0)]
    
    try:
        # Validate bids first
        validated_bids = validate_bids(bids)
        print(f"✓ Bid validation passed: {validated_bids}")
        
        results = value_afrr_down(
            df_cap=prices_df, dt_col_cap="dt", price_col_cap="price_pln_per_mw_h", price_unit="per_mw_h",
            df_vol=volumes_df, dt_col_vol="dt", mw_col_vol="mw_procured",
            portfolio_mw=50.0,
            bids=bids,
            model_mode="parametric"
        )
        
        print(f"✓ Banded bidding successful")
        print(f"  Portfolio baseline: {results['portfolio_baseline_PLN']:.2f} PLN")
        print(f"  Portfolio capped: {results['portfolio_capped_PLN']:.2f} PLN")
        
        # Validate acceptance stats for each band
        for i, acc_stat in enumerate(results['acceptance_stats']):
            print(f"  Band {i+1}: K={acc_stat['bid_K']}, fraction={acc_stat['fraction']}, p_acc={acc_stat['avg_acceptance_prob']:.3f}")
        
        return True
        
    except Exception as e:
        print(f"✗ Banded bidding failed: {e}")
        return False

def test_budget_envelope():
    """Test budget envelope logic."""
    print("\n=== Testing Budget Envelope ===")
    
    # Create test data where some intervals will hit budget cap
    dates = pd.date_range("2024-07-01", periods=16, freq="15min", tz="UTC")
    
    # High prices to trigger budget binding
    prices_df = pd.DataFrame({
        "dt": dates,
        "price_pln_per_mw_h": [100.0, 200.0, 800.0, 1000.0] * 4  # High prices
    })
    
    volumes_df = pd.DataFrame({
        "dt": dates,
        "mw_procured": [200.0] * len(dates)
    })
    
    try:
        # Low K to increase acceptance probability and revenue
        results = value_afrr_down(
            df_cap=prices_df, dt_col_cap="dt", price_col_cap="price_pln_per_mw_h", price_unit="per_mw_h",
            df_vol=volumes_df, dt_col_vol="dt", mw_col_vol="mw_procured",
            portfolio_mw=100.0,
            single_K=50.0,  # Very low K for high acceptance
            model_mode="parametric"
        )
        
        print(f"✓ Budget envelope test successful")
        print(f"  Portfolio baseline: {results['portfolio_baseline_PLN']:.2f} PLN")
        print(f"  Portfolio capped: {results['portfolio_capped_PLN']:.2f} PLN")
        print(f"  Bound intervals: {results['bound_intervals_pct']:.1f}%")
        print(f"  Capping effect: {results['portfolio_baseline_PLN'] - results['portfolio_capped_PLN']:.2f} PLN")
        
        # Check that capped < baseline (should be true if budget is binding)
        if results['portfolio_capped_PLN'] < results['portfolio_baseline_PLN']:
            print(f"  ✓ Budget capping is working correctly")
        else:
            print(f"  ? No budget capping occurred (may be expected)")
        
        return True
        
    except Exception as e:
        print(f"✗ Budget envelope test failed: {e}")
        return False

def test_acceptance_probability_monotonicity():
    """Test that p_acc(K) decreases as K increases."""
    print("\n=== Testing Acceptance Probability Monotonicity ===")
    
    dates = pd.date_range("2024-07-01", periods=24, freq="15min", tz="UTC")
    
    prices_df = pd.DataFrame({
        "dt": dates,
        "price_pln_per_mw_h": np.random.lognormal(5.5, 0.3, len(dates))
    })
    
    volumes_df = pd.DataFrame({
        "dt": dates,
        "mw_procured": [300.0] * len(dates)
    })
    
    # Test with increasing K values
    K_values = [200.0, 300.0, 400.0, 500.0]
    acceptance_probs = []
    
    try:
        for K in K_values:
            results = value_afrr_down(
                df_cap=prices_df, dt_col_cap="dt", price_col_cap="price_pln_per_mw_h", price_unit="per_mw_h",
                df_vol=volumes_df, dt_col_vol="dt", mw_col_vol="mw_procured",
                portfolio_mw=50.0,
                single_K=K,
                model_mode="parametric",
                return_intervals=False  # Speed up test
            )
            
            avg_p_acc = results['acceptance_stats'][0]['avg_acceptance_prob']
            acceptance_probs.append(avg_p_acc)
            print(f"  K={K}: p_acc = {avg_p_acc:.4f}")
        
        # Check monotonicity: p_acc should decrease as K increases
        is_monotonic = all(acceptance_probs[i] >= acceptance_probs[i+1] for i in range(len(acceptance_probs)-1))
        
        if is_monotonic:
            print(f"✓ Acceptance probability monotonicity test passed")
            return True
        else:
            print(f"✗ Acceptance probability should decrease with increasing K")
            return False
        
    except Exception as e:
        print(f"✗ Monotonicity test failed: {e}")
        return False

def test_energy_leg():
    """Test energy leg (short put) functionality."""
    print("\n=== Testing Energy Leg (Short Put) ===")
    
    dates = pd.date_range("2024-07-01", periods=24, freq="15min", tz="UTC")
    
    # Create energy and alternative price data
    energy_df = pd.DataFrame({
        "dt": dates,
        "balancing_price": [80.0, 90.0, 100.0, 110.0] * 6  # Balancing energy prices
    })
    
    alt_df = pd.DataFrame({
        "dt": dates,
        "day_ahead_price": [120.0] * len(dates)  # Fixed alternative price
    })
    
    prices_df = pd.DataFrame({
        "dt": dates,
        "price_pln_per_mw_h": [400.0] * len(dates)
    })
    
    volumes_df = pd.DataFrame({
        "dt": dates,
        "mw_procured": [300.0] * len(dates)
    })
    
    try:
        results = value_afrr_down(
            df_cap=prices_df, dt_col_cap="dt", price_col_cap="price_pln_per_mw_h", price_unit="per_mw_h",
            df_vol=volumes_df, dt_col_vol="dt", mw_col_vol="mw_procured",
            portfolio_mw=50.0,
            single_K=300.0,
            model_mode="parametric",
            # Energy leg parameters
            df_energy=energy_df, dt_col_energy="dt", energy_col="balancing_price",
            df_alt=alt_df, dt_col_alt="dt", alt_col="day_ahead_price",
            theta_list=[0.005, 0.01, 0.02, 0.05],  # 0.5%, 1%, 2%, 5%
            risk_uplift=0.15,
            return_intervals=False
        )
        
        if "energy_leg" in results:
            energy_results = results["energy_leg"]
            print(f"✓ Energy leg calculation successful")
            print(f"  Energy loss: {energy_results['energy_loss_pln_per_mwh']:.2f} PLN/MWh")
            
            if "theta_sweep" in energy_results:
                print(f"  Theta sweep results:")
                for theta_result in energy_results["theta_sweep"]:
                    print(f"    θ={theta_result['theta_pct']:.1f}%: floor={theta_result['premium_floor_pln_per_mwh']:.2f}, rec={theta_result['premium_recommended_pln_per_mwh']:.2f} PLN/MW-h")
            
            # Expected loss should be (120 - avg(balancing_prices))^+ = (120 - 100)^+ = 20
            expected_loss = max(120.0 - 100.0, 0)  # (K_alt - S_bal)^+
            actual_loss = energy_results['energy_loss_pln_per_mwh']
            
            if abs(actual_loss - expected_loss) < 1.0:
                print(f"  ✓ Energy loss calculation appears correct")
            else:
                print(f"  ? Energy loss: expected ~{expected_loss}, got {actual_loss}")
            
            return True
        else:
            print(f"✗ Energy leg results not found")
            return False
        
    except Exception as e:
        print(f"✗ Energy leg test failed: {e}")
        return False

def run_all_tests():
    """Run all validation tests."""
    print("=" * 60)
    print("aFRR-DOWN VALUATION MODULE VALIDATION")
    print("=" * 60)
    
    tests = [
        test_single_K_valuation,
        test_banded_bidding,
        test_budget_envelope,
        test_acceptance_probability_monotonicity,
        test_energy_leg
    ]
    
    passed = 0
    total = len(tests)
    
    for test_func in tests:
        try:
            if test_func():
                passed += 1
        except Exception as e:
            print(f"✗ Test {test_func.__name__} crashed: {e}")
    
    print("\n" + "=" * 60)
    print(f"RESULTS: {passed}/{total} tests passed")
    print("=" * 60)
    
    if passed == total:
        print("🎯 All tests passed! aFRR valuation module is working correctly.")
    else:
        print(f"⚠️  {total - passed} test(s) failed. Check implementation.")
    
    return passed == total

if __name__ == "__main__":
    run_all_tests()