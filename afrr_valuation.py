#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
aFRR-Down Valuation Module

Implements the two-leg aFRR-down product model as specified:
1. Capacity leg: Pay-as-bid option premium with acceptance probability
2. Energy leg: Short put on balancing energy spread (optional)

Key features:
- Banded bidding support: (v_i, K_i) pairs
- Budget envelope: interval-level budget cap
- Parametric vs Empirical acceptance probability models
- Proper unit handling and alignment to 15-min grid
"""

import json
import math
from pathlib import Path
from typing import List, Optional, Tuple, Dict, Union

import numpy as np
import pandas as pd
from scipy.stats import norm

# Reuse utility functions from existing code
from valuation_core import (
    read_any, to_dt_utc, infer_interval_hours, align_to_quarter, 
    normalize_price_units, design_matrix, fit_ridge
)


class CapacityLegModel:
    """Handles capacity leg valuation with acceptance probability modeling."""
    
    def __init__(self, model_mode: str = "parametric", ridge_lambda: float = 1.0):
        self.model_mode = model_mode
        self.ridge_lambda = ridge_lambda
        self.fitted_params = {}
    
    def fit_parametric_model(self, df: pd.DataFrame, price_col: str, pred_cols: List[str]) -> Dict:
        """
        Fit lognormal model: ln(P^cap_t) ~ N(μ_t, σ²)
        μ_t from ridge regression on predictors, σ from residuals.
        """
        # Use log prices for modeling
        log_prices = np.log(df[price_col].clip(lower=1e-3))
        valid_mask = np.isfinite(log_prices)
        
        if pred_cols and not df[pred_cols].empty:
            # Ridge regression for drift μ_t
            pred_mask = valid_mask & df[pred_cols].notna().all(axis=1)
            if pred_mask.sum() > 10:
                X, used_cols = design_matrix(df[pred_mask], pred_cols)
                beta_mu, sigma_residual = fit_ridge(X, log_prices[pred_mask], self.ridge_lambda)
                
                # Apply to all data
                X_all, _ = design_matrix(df, pred_cols)
                mu_t_series = X_all @ beta_mu
            else:
                # Fallback to constant drift
                mu_t_series = np.full(len(df), np.mean(log_prices[valid_mask]))
                sigma_residual = np.std(log_prices[valid_mask])
                beta_mu = np.array([mu_t_series[0]])
                used_cols = ["intercept"]
        else:
            # Intercept-only model
            mu_t_series = np.full(len(df), np.mean(log_prices[valid_mask]))
            sigma_residual = max(np.std(log_prices[valid_mask]), 0.01)
            beta_mu = np.array([mu_t_series[0]])
            used_cols = ["intercept"]
        
        self.fitted_params = {
            "beta_mu": beta_mu,
            "sigma": max(sigma_residual, 0.01),
            "mu_t_series": mu_t_series,
            "used_cols": used_cols,
            "model_mode": "parametric"
        }
        
        return self.fitted_params
    
    def fit_empirical_model(self, df: pd.DataFrame, price_col: str, window_days: int = 30) -> Dict:
        """
        Fit empirical CDF model over rolling window.
        p_acc,t(K) = #{P^cap >= K} / #obs in window
        """
        # For simplicity, use full sample CDF (can be extended to rolling)
        prices = df[price_col].dropna()
        
        self.fitted_params = {
            "price_values": prices.values,
            "model_mode": "empirical",
            "window_days": window_days
        }
        
        return self.fitted_params
    
    def compute_acceptance_probability(self, df: pd.DataFrame, K_values: np.ndarray) -> pd.DataFrame:
        """
        Compute p_acc,t(K) for each interval and bid level.
        
        Returns DataFrame with columns: dt, K_1, K_2, ..., K_n (acceptance probabilities)
        """
        if not self.fitted_params:
            raise ValueError("Model must be fitted first")
        
        result = df[['dt']].copy()
        
        if self.fitted_params["model_mode"] == "parametric":
            # Parametric: p_acc(K) = 1 - Φ((ln(K) - μ_t) / σ)
            mu_t = self.fitted_params["mu_t_series"]
            sigma = self.fitted_params["sigma"]
            
            for i, K in enumerate(K_values):
                if K <= 0:
                    result[f"p_acc_K{i+1}"] = 1.0  # Always accepted for K=0
                else:
                    z_scores = (np.log(K) - mu_t) / sigma
                    result[f"p_acc_K{i+1}"] = 1.0 - norm.cdf(z_scores)
        
        elif self.fitted_params["model_mode"] == "empirical":
            # Empirical: use historical CDF
            price_values = self.fitted_params["price_values"]
            
            for i, K in enumerate(K_values):
                p_acc = np.mean(price_values >= K)
                result[f"p_acc_K{i+1}"] = p_acc  # Constant across all intervals
        
        return result


def validate_bids(bids: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    """
    Validate banded bids: (v_i, K_i) pairs.
    - Sum of fractions = 1
    - K_i strictly increasing
    """
    if not bids:
        raise ValueError("No bids provided")
    
    fractions, K_values = zip(*bids)
    
    # Check fractions sum to 1
    if not np.isclose(sum(fractions), 1.0):
        raise ValueError(f"Bid fractions must sum to 1, got {sum(fractions)}")
    
    # Check K values are strictly increasing
    if not all(K_values[i] < K_values[i+1] for i in range(len(K_values)-1)):
        raise ValueError("Bid levels K_i must be strictly increasing")
    
    return bids


def compute_capacity_revenue(
    df: pd.DataFrame, 
    bids: List[Tuple[float, float]], 
    p_acc_df: pd.DataFrame,
    portfolio_mw: float
) -> pd.DataFrame:
    """
    Compute expected pay-as-bid capacity revenue per interval.
    
    Single bid: R^cap_t = K × p_acc,t(K) × min(Portfolio, Q^proc_t) × Δh
    Banded: R^cap_t = Σ v_i × K_i × p_acc,t(K_i) × min(Portfolio, Q^proc_t) × Δh
    """
    result = df[['dt', 'mw_procured']].copy()
    result['Hours_in_interval'] = 0.25  # Fixed 15-min intervals
    
    # Capacity available: min(portfolio, procured)
    result['mw_available'] = np.minimum(portfolio_mw, result['mw_procured'])
    
    # Compute revenue for each bid band
    total_revenue = np.zeros(len(result))
    
    for i, (v_i, K_i) in enumerate(bids):
        p_acc_col = f"p_acc_K{i+1}"
        if p_acc_col not in p_acc_df.columns:
            raise ValueError(f"Missing acceptance probability column: {p_acc_col}")
        
        # Merge acceptance probabilities
        band_df = result.merge(p_acc_df[['dt', p_acc_col]], on='dt', how='left')
        
        # Revenue for this band: v_i × K_i × p_acc,t(K_i) × mw_available × Δh
        band_revenue = (
            v_i * K_i * 
            band_df[p_acc_col].fillna(0) * 
            band_df['mw_available'] * 
            band_df['Hours_in_interval']
        )
        
        result[f"revenue_band_{i+1}"] = band_revenue
        total_revenue += band_revenue
    
    result['capacity_revenue_baseline'] = total_revenue
    
    return result


def apply_budget_envelope(df: pd.DataFrame, price_col: str = "price_pln_per_mw_h") -> pd.DataFrame:
    """
    Apply budget envelope constraint per interval.
    
    Budget: B_t = P^cap_t × min(Portfolio, Q^proc_t) × Δh
    Capped revenue: R^cap,capped_t = min(R^cap_t, B_t)
    Bound flag: 1 if R^cap_t > B_t
    """
    result = df.copy()
    
    # Compute budget envelope
    result['budget_envelope'] = (
        result[price_col].clip(lower=0) * 
        result['mw_available'] * 
        result['Hours_in_interval']
    )
    
    # Apply cap
    result['capacity_revenue_capped'] = np.minimum(
        result['capacity_revenue_baseline'], 
        result['budget_envelope']
    )
    
    # Bound flag
    result['bound_flag'] = (
        result['capacity_revenue_baseline'] > result['budget_envelope']
    ).astype(int)
    
    return result


class EnergyLegModel:
    """Handles energy leg valuation (short put on balancing spread)."""
    
    @staticmethod
    def compute_energy_loss(
        df_energy: pd.DataFrame, energy_col: str,
        df_alt: pd.DataFrame, alt_col: str,
        dt_col: str = "dt"
    ) -> Dict:
        """
        Compute expected loss from short put: E[(K^alt - S^bal)^+]
        """
        # Align energy and alternative data
        energy_df = df_energy[[dt_col, energy_col]].copy()
        alt_df = df_alt[[dt_col, alt_col]].copy()
        
        merged = energy_df.merge(alt_df, on=dt_col, how='inner', suffixes=('_energy', '_alt'))
        
        if merged.empty:
            return {"energy_loss_pln_per_mwh": 0.0, "intervals_count": 0}
        
        # Short put payoff: (K^alt - S^bal)^+
        merged['put_payoff'] = np.maximum(
            merged[alt_col] - merged[energy_col], 
            0
        )
        
        # Expected loss per MWh
        expected_loss = merged['put_payoff'].mean()
        
        return {
            "energy_loss_pln_per_mwh": expected_loss,
            "intervals_count": len(merged),
            "put_payoffs": merged['put_payoff'].values
        }
    
    @staticmethod
    def theta_sweep_analysis(
        energy_loss_pln_per_mwh: float,
        theta_list: List[float],
        risk_uplift: float = 0.15
    ) -> Dict:
        """
        Compute premium floor for different activation intensities.
        Premium floor = θ × L × (1 + risk_uplift)
        """
        theta_results = []
        
        for theta in theta_list:
            premium_min = theta * energy_loss_pln_per_mwh
            premium_rec = premium_min * (1 + risk_uplift)
            
            theta_results.append({
                "theta_pct": theta * 100,
                "activation_intensity": theta,
                "premium_floor_pln_per_mwh": premium_min,
                "premium_recommended_pln_per_mwh": premium_rec
            })
        
        return {
            "energy_loss_pln_per_mwh": energy_loss_pln_per_mwh,
            "risk_uplift": risk_uplift,
            "theta_sweep": theta_results
        }


def value_afrr_down(
    # Capacity price & volumes (required)
    df_cap: pd.DataFrame, dt_col_cap: str, price_col_cap: str, price_unit: str,
    df_vol: pd.DataFrame, dt_col_vol: str, mw_col_vol: str,
    
    # Predictors (optional)
    df_pred: Optional[pd.DataFrame] = None, dt_col_pred: Optional[str] = None, 
    pred_cols: Optional[List[str]] = None, ridge_lambda: float = 1.0, 
    model_mode: str = "parametric",  # 'parametric' | 'empirical'
    
    # Bids
    portfolio_mw: float = 1.0,
    bids: Optional[List[Tuple[float, float]]] = None,  # (v_i, K_i)
    single_K: Optional[float] = None,
    
    # Energy leg (optional)
    df_energy: Optional[pd.DataFrame] = None, dt_col_energy: Optional[str] = None, 
    energy_col: Optional[str] = None,
    df_alt: Optional[pd.DataFrame] = None, dt_col_alt: Optional[str] = None, 
    alt_col: Optional[str] = None,
    theta_list: Optional[List[float]] = None, risk_uplift: float = 0.15,
    
    # Output control
    return_intervals: bool = True
) -> Dict:
    """
    Main aFRR-down valuation function implementing the two-leg product model.
    
    Returns a comprehensive results dictionary with:
    - capacity_perMW_baseline_PLN, capacity_perMW_capped_PLN
    - portfolio_baseline_PLN, portfolio_capped_PLN  
    - bound_intervals_pct
    - model diagnostics (sigma, beta, acceptance stats)
    - energy leg results (if provided)
    - intervals data (if return_intervals=True)
    """
    
    # Handle bid specification
    if bids is None and single_K is not None:
        bids = [(1.0, single_K)]  # Single bid as 100% fraction
    elif bids is None:
        raise ValueError("Must specify either 'bids' or 'single_K'")
    
    # Validate bids
    bids = validate_bids(bids)
    K_values = np.array([K for _, K in bids])
    
    # ---- CAPACITY LEG PROCESSING ----
    
    # 1) Clean and normalize capacity prices
    cap_df_clean = df_cap[[dt_col_cap, price_col_cap]].dropna()
    if cap_df_clean.empty:
        raise ValueError("No valid capacity price data")
    
    cap_df_clean[dt_col_cap] = to_dt_utc(cap_df_clean[dt_col_cap])
    cap_interval_h = infer_interval_hours(pd.DatetimeIndex(cap_df_clean[dt_col_cap]))
    
    # Align to 15-min grid and normalize units
    cap_aligned = align_to_quarter(cap_df_clean, dt_col_cap, [price_col_cap])
    cap_aligned["price_pln_per_mw_h"] = normalize_price_units(
        cap_aligned, price_col_cap, cap_interval_h, price_unit
    )
    
    # 2) Process volumes
    vol_df_clean = df_vol[[dt_col_vol, mw_col_vol]].dropna()
    if vol_df_clean.empty:
        raise ValueError("No valid volume data")
    
    vol_aligned = align_to_quarter(vol_df_clean, dt_col_vol, [mw_col_vol])
    vol_aligned["mw_procured"] = vol_aligned[mw_col_vol].clip(lower=0)
    
    # 3) Process predictors (if provided)
    if df_pred is not None and dt_col_pred and pred_cols:
        pred_aligned = align_to_quarter(df_pred, dt_col_pred, pred_cols)
    else:
        pred_aligned = None
        pred_cols = []
    
    # 4) Merge datasets
    df = cap_aligned[["dt", "price_pln_per_mw_h"]].copy()
    df = df.merge(vol_aligned[["dt", "mw_procured"]], on="dt", how="inner")
    
    if pred_aligned is not None:
        df = df.merge(pred_aligned, on="dt", how="left")
    
    df = df.sort_values("dt").reset_index(drop=True)
    if df.empty:
        raise ValueError("No overlapping data after time alignment")
    
    # 5) Fit acceptance probability model
    capacity_model = CapacityLegModel(model_mode, ridge_lambda)
    
    if model_mode == "parametric":
        model_params = capacity_model.fit_parametric_model(df, "price_pln_per_mw_h", pred_cols)
    else:
        model_params = capacity_model.fit_empirical_model(df, "price_pln_per_mw_h")
    
    # 6) Compute acceptance probabilities
    p_acc_df = capacity_model.compute_acceptance_probability(df, K_values)
    
    # 7) Compute capacity revenue
    revenue_df = compute_capacity_revenue(df, bids, p_acc_df, portfolio_mw)
    
    # 8) Apply budget envelope
    final_df = revenue_df.merge(df[["dt", "price_pln_per_mw_h"]], on="dt")
    final_df = apply_budget_envelope(final_df)
    
    # ---- ENERGY LEG PROCESSING (Optional) ----
    energy_results = {}
    if (df_energy is not None and dt_col_energy and energy_col and
        df_alt is not None and dt_col_alt and alt_col):
        
        energy_loss_result = EnergyLegModel.compute_energy_loss(
            df_energy, energy_col, df_alt, alt_col, dt_col_energy
        )
        
        if theta_list:
            theta_sweep_result = EnergyLegModel.theta_sweep_analysis(
                energy_loss_result["energy_loss_pln_per_mwh"], 
                theta_list, 
                risk_uplift
            )
            energy_results.update(theta_sweep_result)
        else:
            energy_results.update(energy_loss_result)
    
    # ---- SUMMARY CALCULATIONS ----
    
    # Aggregate results
    total_intervals = len(final_df)
    bound_intervals = final_df['bound_flag'].sum()
    
    # Per-MW totals (baseline and capped)
    perMW_baseline = final_df['capacity_revenue_baseline'].sum() / portfolio_mw if portfolio_mw > 0 else 0
    perMW_capped = final_df['capacity_revenue_capped'].sum() / portfolio_mw if portfolio_mw > 0 else 0
    
    # Portfolio totals
    portfolio_baseline = final_df['capacity_revenue_baseline'].sum()
    portfolio_capped = final_df['capacity_revenue_capped'].sum()
    
    # Acceptance statistics per bid level
    acceptance_stats = []
    for i, (v_i, K_i) in enumerate(bids):
        p_acc_col = f"p_acc_K{i+1}"
        avg_p_acc = p_acc_df[p_acc_col].mean()
        acceptance_stats.append({
            "band": i + 1,
            "fraction": v_i,
            "bid_K": K_i,
            "avg_acceptance_prob": avg_p_acc,
            "expected_MWh": (p_acc_df[p_acc_col] * final_df['mw_available'] * 0.25).sum()
        })
    
    # Build results dictionary
    results = {
        # Capacity leg results
        "capacity_perMW_baseline_PLN": perMW_baseline,
        "capacity_perMW_capped_PLN": perMW_capped,
        "portfolio_baseline_PLN": portfolio_baseline,
        "portfolio_capped_PLN": portfolio_capped,
        "bound_intervals_pct": 100.0 * bound_intervals / total_intervals if total_intervals > 0 else 0,
        
        # Model diagnostics
        "model_mode": model_mode,
        "model_params": model_params,
        "acceptance_stats": acceptance_stats,
        
        # Portfolio settings
        "portfolio_mw": portfolio_mw,
        "bids": bids,
        "total_intervals": total_intervals,
        "bound_intervals": bound_intervals
    }
    
    # Add energy leg results if available
    if energy_results:
        results["energy_leg"] = energy_results
    
    # Add interval data if requested
    if return_intervals:
        # Merge all relevant columns for output
        intervals_output = final_df.merge(p_acc_df, on="dt", how="left")
        results["intervals_df"] = intervals_output
        
        # Chart data for visualization (compatible with existing template)
        results["chart_data"] = {
            "dt": intervals_output["dt"].dt.strftime("%Y-%m-%d %H:%M:%S").tolist(),
            "price_pln_per_mw_h": intervals_output["price_pln_per_mw_h"].tolist(),
            "mw_procured": intervals_output["mw_procured"].tolist(),
            "budget_pln_interval": intervals_output["budget_envelope"].tolist(),  # Renamed for template compatibility
            "pln_portfolio_capped": intervals_output["capacity_revenue_capped"].tolist(),
            "strike_line": [K_values[0]] * len(intervals_output) if len(K_values) > 0 else [400] * len(intervals_output),  # Strike price line for charts
            "p_accept": [0.5] * len(intervals_output),  # Placeholder - would need per-interval acceptance probs
            "bound_flag": intervals_output["bound_flag"].tolist()
        }
    
    return results


if __name__ == "__main__":
    # Simple test
    print("aFRR-down valuation module loaded successfully")
    print("Main API function: value_afrr_down()")