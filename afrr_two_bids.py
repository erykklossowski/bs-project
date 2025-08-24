#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
aFRR-Down Two-Bid Valuation Module

Implements the complete two-bid model as specified:
1. Capacity leg: K_cap (option premium bid) with pay-as-bid revenue
2. Energy leg: K_energy (energy strike bid) with configurable pay rules

Key features:
- Separate capacity and energy bid parameters
- Energy acceptance probability modeling
- Configurable energy pay rules (difference, pay_as_bid, pay_as_clear)
- Support for negative energy bids
- Two-parameter exploration capabilities
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
    """Handles capacity leg valuation with K_cap bidding."""
    
    def __init__(self, cap_model: str = "parametric", ridge_lambda: float = 1.0):
        self.cap_model = cap_model
        self.ridge_lambda = ridge_lambda
        self.fitted_params = {}
    
    def fit_capacity_model(self, df: pd.DataFrame, price_col: str, pred_cols: List[str]) -> Dict:
        """
        Fit capacity price model: ln(P^cap_t) ~ N(μ_t, σ²)
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
            "cap_model": "parametric"
        }
        
        return self.fitted_params
    
    def compute_capacity_acceptance_probability(self, df: pd.DataFrame, K_cap_values: np.ndarray) -> pd.DataFrame:
        """
        Compute p_cap,t(K_cap) = Pr(P^cap_t >= K_cap) for each interval and bid level.
        """
        if not self.fitted_params:
            raise ValueError("Capacity model must be fitted first")
        
        result = df[['dt']].copy()
        
        if self.fitted_params["cap_model"] == "parametric":
            # Parametric: p_cap(K_cap) = 1 - Φ((ln(K_cap) - μ_t) / σ)
            mu_t = self.fitted_params["mu_t_series"]
            sigma = self.fitted_params["sigma"]
            
            for i, K_cap in enumerate(K_cap_values):
                if K_cap <= 0:
                    result[f"p_cap_K{i+1}"] = 1.0  # Always accepted for K_cap=0
                else:
                    z_scores = (np.log(K_cap) - mu_t) / sigma
                    result[f"p_cap_K{i+1}"] = 1.0 - norm.cdf(z_scores)
        
        return result


class EnergyLegModel:
    """Handles energy leg valuation with K_energy bidding."""
    
    @staticmethod
    def fit_energy_acceptance_model(df_energy: pd.DataFrame, energy_col: str) -> Dict:
        """
        Fit empirical CDF for energy acceptance probability.
        Energy prices can be negative, so avoid lognormal.
        """
        energy_prices = df_energy[energy_col].dropna()
        
        return {
            "energy_prices": energy_prices.values,
            "model_type": "empirical_cdf"
        }
    
    @staticmethod
    def compute_energy_acceptance_probability(
        energy_model: Dict, 
        df: pd.DataFrame, 
        K_energy: float
    ) -> pd.Series:
        """
        Compute p_eng,t(K_energy) ≈ Pr(S^bal_t >= K_energy).
        For down-regulation, more negative K_energy is cheaper → higher acceptance.
        """
        if energy_model["model_type"] == "empirical_cdf":
            energy_prices = energy_model["energy_prices"]
            # Empirical probability that market price >= our bid
            p_energy_accept = np.mean(energy_prices >= K_energy)
            return pd.Series([p_energy_accept] * len(df), index=df.index)
        else:
            # Fallback: 50% acceptance
            return pd.Series([0.5] * len(df), index=df.index)
    
    @staticmethod
    def compute_energy_payoff(
        df_energy: pd.DataFrame, 
        energy_col: str, 
        K_energy: float, 
        energy_pay_rule: str = "difference"
    ) -> pd.Series:
        """
        Compute energy payoff per MWh based on pay rule:
        - difference: S_bal,t - K_energy
        - pay_as_bid: K_energy  
        - pay_as_clear: S_bal,t
        """
        S_bal = df_energy[energy_col]
        
        if energy_pay_rule == "difference":
            return S_bal - K_energy
        elif energy_pay_rule == "pay_as_bid":
            return pd.Series([K_energy] * len(S_bal), index=S_bal.index)
        elif energy_pay_rule == "pay_as_clear":
            return S_bal
        else:
            raise ValueError(f"Unknown energy pay rule: {energy_pay_rule}")


def validate_capacity_bands(cap_bands: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    """
    Validate capacity banded bids: (v_i, K_cap_i) pairs.
    - Sum of fractions = 1
    - K_cap_i strictly increasing
    """
    if not cap_bands:
        raise ValueError("No capacity bands provided")
    
    fractions, K_cap_values = zip(*cap_bands)
    
    # Check fractions sum to 1
    if not np.isclose(sum(fractions), 1.0):
        raise ValueError(f"Capacity bid fractions must sum to 1, got {sum(fractions)}")
    
    # Check K_cap values are strictly increasing
    if not all(K_cap_values[i] < K_cap_values[i+1] for i in range(len(K_cap_values)-1)):
        raise ValueError("Capacity bid levels K_cap_i must be strictly increasing")
    
    return cap_bands


def compute_capacity_revenue(
    df: pd.DataFrame, 
    cap_bands: List[Tuple[float, float]], 
    p_cap_df: pd.DataFrame,
    portfolio_mw: float
) -> pd.DataFrame:
    """
    Compute expected pay-as-bid capacity revenue per interval.
    R^cap_t = Σ v_i × K_cap_i × p_cap,t(K_cap_i) × min(Portfolio, Q^proc_t) × Δh
    """
    result = df[['dt', 'mw_procured']].copy()
    result['Hours_in_interval'] = 0.25  # Fixed 15-min intervals
    
    # Capacity available: min(portfolio, procured)
    result['mw_available'] = np.minimum(portfolio_mw, result['mw_procured'])
    
    # Compute revenue for each capacity bid band
    total_revenue = np.zeros(len(result))
    
    for i, (v_i, K_cap_i) in enumerate(cap_bands):
        p_cap_col = f"p_cap_K{i+1}"
        if p_cap_col not in p_cap_df.columns:
            raise ValueError(f"Missing capacity acceptance probability column: {p_cap_col}")
        
        # Merge acceptance probabilities
        band_df = result.merge(p_cap_df[['dt', p_cap_col]], on='dt', how='left')
        
        # Revenue for this band: v_i × K_cap_i × p_cap,t(K_cap_i) × mw_available × Δh
        band_revenue = (
            v_i * K_cap_i * 
            band_df[p_cap_col].fillna(0) * 
            band_df['mw_available'] * 
            band_df['Hours_in_interval']
        )
        
        result[f"cap_revenue_band_{i+1}"] = band_revenue
        total_revenue += band_revenue
    
    result['capacity_revenue_baseline'] = total_revenue
    
    return result


def compute_energy_revenue(
    df: pd.DataFrame,
    cap_bands: List[Tuple[float, float]],
    p_cap_df: pd.DataFrame,
    energy_model: Dict,
    K_energy: float,
    energy_payoff: pd.Series,
    theta: float,
    portfolio_mw: float
) -> pd.DataFrame:
    """
    Compute expected energy revenue per interval.
    R^eng_t = [Σ v_i × p_cap,t(K_cap_i)] × p_eng,t(K_energy) × θ × min(Portfolio, Q^proc_t) × payoff_per_MWh × Δh
    """
    result = df[['dt', 'mw_procured']].copy()
    result['Hours_in_interval'] = 0.25
    result['mw_available'] = np.minimum(portfolio_mw, result['mw_procured'])
    
    # Compute overall capacity acceptance probability (weighted average)
    total_cap_prob = np.zeros(len(result))
    
    for i, (v_i, K_cap_i) in enumerate(cap_bands):
        p_cap_col = f"p_cap_K{i+1}"
        if p_cap_col in p_cap_df.columns:
            band_df = result.merge(p_cap_df[['dt', p_cap_col]], on='dt', how='left')
            total_cap_prob += v_i * band_df[p_cap_col].fillna(0).values
    
    # Compute energy acceptance probability
    p_energy_series = EnergyLegModel.compute_energy_acceptance_probability(
        energy_model, result, K_energy
    )
    
    # Energy revenue per interval
    energy_revenue = (
        total_cap_prob *  # Must be capacity selected first
        p_energy_series.values * 
        theta *  # Activation rate
        result['mw_available'].values * 
        energy_payoff.values * 
        result['Hours_in_interval'].values
    )
    
    result['energy_revenue'] = energy_revenue
    result['p_cap_weighted'] = total_cap_prob
    result['p_energy'] = p_energy_series
    result['payoff_per_MWh'] = energy_payoff
    
    return result


def apply_budget_envelope(df: pd.DataFrame, price_col: str = "price_pln_per_mw_h") -> pd.DataFrame:
    """
    Apply budget envelope constraint per interval.
    B_t = P^cap_t × min(Portfolio, Q^proc_t) × Δh
    """
    result = df.copy()
    
    # Compute budget envelope
    result['budget_envelope'] = (
        result[price_col].clip(lower=0) * 
        result['mw_available'] * 
        result['Hours_in_interval']
    )
    
    # Apply cap to capacity revenue only (check if column exists)
    if 'capacity_revenue_baseline' in result.columns:
        result['capacity_revenue_capped'] = np.minimum(
            result['capacity_revenue_baseline'], 
            result['budget_envelope']
        )
        
        # Bound flag
        result['bound_flag'] = (
            result['capacity_revenue_baseline'] > result['budget_envelope']
        ).astype(int)
        
        # Total revenue = capped capacity + energy
        result['total_revenue'] = (
            result['capacity_revenue_capped'] + 
            result.get('energy_revenue', 0)
        )
    else:
        # If no capacity revenue column, just use energy revenue
        result['capacity_revenue_capped'] = 0
        result['bound_flag'] = 0
        result['total_revenue'] = result.get('energy_revenue', 0)
    
    return result


def value_afrr_down_two_bids(
    # Capacity data
    df_cap: pd.DataFrame, dt_cap: str, col_cap: str, cap_unit: str,
    df_vol: pd.DataFrame, dt_vol: str, col_vol_mw: str,
    
    # Predictors (optional)
    df_pred: Optional[pd.DataFrame] = None, dt_pred: Optional[str] = None, 
    pred_cols: Optional[List[str]] = None,
    ridge_lambda: float = 1.0, cap_model: str = "parametric",
    
    # Capacity bids
    portfolio_mw: float = 1.0,
    cap_bands: Optional[List[Tuple[float, float]]] = None,
    single_K_cap: Optional[float] = None,
    
    # Energy inputs/bid
    df_energy: Optional[pd.DataFrame] = None, dt_eng: Optional[str] = None, 
    col_energy: Optional[str] = None,
    single_K_energy: Optional[float] = None,
    energy_pay_rule: str = "difference",
    theta: float = 0.01,
    
    # Options
    return_intervals: bool = True,
) -> Dict:
    """
    Main two-bid aFRR-down valuation function.
    
    Returns comprehensive results dictionary with:
    - Separate capacity and energy leg results
    - Total combined revenue
    - Detailed interval data for analysis
    """
    
    # Handle capacity bid specification
    if cap_bands is None and single_K_cap is not None:
        cap_bands = [(1.0, single_K_cap)]  # Single bid as 100% fraction
    elif cap_bands is None:
        raise ValueError("Must specify either 'cap_bands' or 'single_K_cap'")
    
    # Validate capacity bands
    cap_bands = validate_capacity_bands(cap_bands)
    K_cap_values = np.array([K for _, K in cap_bands])
    
    # ---- CAPACITY LEG PROCESSING ----
    
    # 1) Clean and normalize capacity prices
    cap_df_clean = df_cap[[dt_cap, col_cap]].dropna()
    if cap_df_clean.empty:
        raise ValueError("No valid capacity price data")
    
    cap_df_clean[dt_cap] = to_dt_utc(cap_df_clean[dt_cap])
    cap_interval_h = infer_interval_hours(pd.DatetimeIndex(cap_df_clean[dt_cap]))
    
    # Align to 15-min grid and normalize units
    cap_aligned = align_to_quarter(cap_df_clean, dt_cap, [col_cap])
    cap_aligned["price_pln_per_mw_h"] = normalize_price_units(
        cap_aligned, col_cap, cap_interval_h, cap_unit
    )
    
    # 2) Process volumes
    vol_df_clean = df_vol[[dt_vol, col_vol_mw]].dropna()
    if vol_df_clean.empty:
        raise ValueError("No valid volume data")
    
    vol_aligned = align_to_quarter(vol_df_clean, dt_vol, [col_vol_mw])
    vol_aligned["mw_procured"] = vol_aligned[col_vol_mw].clip(lower=0)
    
    # 3) Process predictors (if provided)
    if df_pred is not None and dt_pred and pred_cols:
        pred_aligned = align_to_quarter(df_pred, dt_pred, pred_cols)
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
    
    # 5) Fit capacity acceptance probability model
    capacity_model = CapacityLegModel(cap_model, ridge_lambda)
    model_params = capacity_model.fit_capacity_model(df, "price_pln_per_mw_h", pred_cols)
    
    # 6) Compute capacity acceptance probabilities
    p_cap_df = capacity_model.compute_capacity_acceptance_probability(df, K_cap_values)
    
    # 7) Compute capacity revenue
    revenue_df = compute_capacity_revenue(df, cap_bands, p_cap_df, portfolio_mw)
    
    # ---- ENERGY LEG PROCESSING ----
    energy_results = {}
    
    if (df_energy is not None and dt_eng and col_energy and single_K_energy is not None):
        # Process energy data
        energy_df_clean = df_energy[[dt_eng, col_energy]].dropna()
        energy_df_clean[dt_eng] = to_dt_utc(energy_df_clean[dt_eng])
        energy_aligned = align_to_quarter(energy_df_clean, dt_eng, [col_energy])
        
        # Merge energy data with main dataset
        df_with_energy = revenue_df.merge(
            energy_aligned[["dt", col_energy]], 
            on="dt", 
            how="left"
        ).ffill()  # Forward fill energy prices
        
        # Fit energy acceptance model
        energy_model = EnergyLegModel.fit_energy_acceptance_model(energy_aligned, col_energy)
        
        # Compute energy payoff per MWh
        energy_payoff = EnergyLegModel.compute_energy_payoff(
            df_with_energy, col_energy, single_K_energy, energy_pay_rule
        )
        
        # Compute energy revenue
        energy_revenue_df = compute_energy_revenue(
            df_with_energy, cap_bands, p_cap_df, energy_model,
            single_K_energy, energy_payoff, theta, portfolio_mw
        )
        
        # Merge energy results back into revenue dataframe
        revenue_df = revenue_df.merge(
            energy_revenue_df[['dt', 'energy_revenue', 'p_cap_weighted', 'p_energy', 'payoff_per_MWh']], 
            on='dt', 
            how='left'
        ).fillna(0)
        
        # Energy leg summary
        energy_results = {
            "K_energy": single_K_energy,
            "energy_pay_rule": energy_pay_rule,
            "theta": theta,
            "energy_pln_portfolio": energy_revenue_df['energy_revenue'].sum(),
            "energy_pln_perMW": energy_revenue_df['energy_revenue'].sum() / portfolio_mw if portfolio_mw > 0 else 0,
            "avg_payoff_per_MWh": energy_payoff.mean()
        }
    
    # 8) Apply budget envelope and compute totals
    final_df = revenue_df.merge(df[["dt", "price_pln_per_mw_h"]], on="dt", how="left")
    final_df = apply_budget_envelope(final_df)
    
    # ---- SUMMARY CALCULATIONS ----
    
    # Aggregate results
    total_intervals = len(final_df)
    bound_intervals = final_df['bound_flag'].sum()
    
    # Capacity leg results
    cap_perMW_baseline = final_df['capacity_revenue_baseline'].sum() / portfolio_mw if portfolio_mw > 0 else 0
    cap_perMW_capped = final_df['capacity_revenue_capped'].sum() / portfolio_mw if portfolio_mw > 0 else 0
    cap_portfolio_baseline = final_df['capacity_revenue_baseline'].sum()
    cap_portfolio_capped = final_df['capacity_revenue_capped'].sum()
    
    # Total results (capacity + energy)
    total_portfolio = final_df['total_revenue'].sum()
    
    # Capacity acceptance statistics
    acceptance_stats = []
    for i, (v_i, K_cap_i) in enumerate(cap_bands):
        p_cap_col = f"p_cap_K{i+1}"
        if p_cap_col in p_cap_df.columns:
            avg_p_cap = p_cap_df[p_cap_col].mean()
            expected_MWh = (p_cap_df[p_cap_col] * final_df['mw_available'] * 0.25).sum()
            acceptance_stats.append({
                "band": i + 1,
                "fraction": v_i,
                "K_cap": K_cap_i,
                "avg_acceptance_prob": avg_p_cap,
                "expected_MWh": expected_MWh
            })
    
    # Calculate additional summary statistics - ensure all values are safe numeric types
    def safe_float(val, default=0.0):
        """Safely convert value to float, handling numpy arrays and None."""
        if val is None:
            return default
        if hasattr(val, 'item'):  # numpy scalar
            return float(val.item())
        if hasattr(val, '__len__') and len(val) == 1:  # single-element array
            return float(val[0])
        try:
            return float(val)
        except:
            return default
    
    total_capacity_revenue = safe_float(cap_portfolio_capped)
    total_energy_revenue = safe_float(energy_results.get('expected_energy_revenue_pln', 0) if energy_results else 0)
    per_mw_revenue = safe_float(total_portfolio / portfolio_mw if portfolio_mw > 0 else 0)
    avg_capacity_acceptance = safe_float(np.mean([stat.get('avg_acceptance_prob', 0) for stat in acceptance_stats]) if acceptance_stats else 0)
    bound_intervals_pct = safe_float(100.0 * bound_intervals / total_intervals if total_intervals > 0 else 0)
    
    # Energy leg summary data - ensure all values are safe numeric types
    K_energy_value = safe_float(single_K_energy)
    theta_value = safe_float(theta)
    expected_activations = safe_float(total_intervals * theta_value if theta_value else 0)
    avg_energy_payoff = safe_float(energy_results.get('avg_energy_payoff_per_mwh', 0) if energy_results else 0)
    
    # Build results dictionary
    results = {
        # Model configuration
        "K_cap_values": K_cap_values.tolist(),
        "cap_bands": cap_bands,
        "portfolio_mw": portfolio_mw,
        "cap_model": cap_model,
        "model_params": model_params,
        
        # Template-expected fields for summary display
        "total_capacity_revenue_pln": total_capacity_revenue,
        "total_energy_revenue_pln": total_energy_revenue,
        "total_revenue_pln": total_portfolio,
        "per_mw_revenue_pln": per_mw_revenue,
        "avg_capacity_acceptance_prob": avg_capacity_acceptance,
        "bound_intervals_pct": bound_intervals_pct,
        "energy_pay_rule": energy_pay_rule,
        
        # Energy leg summary
        "K_energy": K_energy_value,
        "theta": theta_value,
        "expected_energy_activations": expected_activations,
        "avg_energy_payoff_per_mwh": avg_energy_payoff,
        
        # Capacity bid stats for table display - ensure all numeric
        "capacity_bid_stats": [
            {
                "band": int(i + 1),
                "fraction": safe_float(stat.get('fraction', 0)),
                "K_cap": safe_float(stat.get('bid_K', K_cap_values[i] if i < len(K_cap_values) else 0)),
                "avg_acceptance_prob": safe_float(stat.get('avg_acceptance_prob', 0)),
                "total_revenue_pln": safe_float(stat.get('expected_MWh', 0) * stat.get('bid_K', 0) * 0.25)  # Rough estimate
            }
            for i, stat in enumerate(acceptance_stats) if stat is not None
        ],
        
        # Legacy fields for compatibility - ensure all numeric
        "capacity_pln_perMW_baseline": safe_float(cap_perMW_baseline),
        "capacity_pln_perMW_capped": safe_float(cap_perMW_capped), 
        "capacity_pln_portfolio_baseline": safe_float(cap_portfolio_baseline),
        "capacity_pln_portfolio_capped": safe_float(cap_portfolio_capped),
        "capacity_acceptance_stats": acceptance_stats or [],
        
        # Total results - ensure all numeric
        "total_pln_portfolio": safe_float(total_portfolio),
        "total_intervals": int(total_intervals) if total_intervals else 0,
        "bound_intervals": int(bound_intervals) if bound_intervals else 0,
    }
    
    # Add energy leg results if available
    if energy_results:
        results["energy_leg"] = energy_results
    
    # Add interval data if requested
    if return_intervals:
        results["intervals_df"] = final_df
        
        # Chart data for visualization
        results["chart_data"] = {
            "dt": final_df["dt"].dt.strftime("%Y-%m-%d %H:%M:%S").tolist(),
            "P_cap": final_df["price_pln_per_mw_h"].tolist(),
            "Q_proc": final_df["mw_procured"].tolist(), 
            "R_cap_baseline": final_df["capacity_revenue_baseline"].tolist(),
            "R_cap_capped": final_df["capacity_revenue_capped"].tolist(),
            "budget_envelope": final_df["budget_envelope"].tolist(),
            "R_total": final_df["total_revenue"].tolist(),
            "bound_flag": final_df["bound_flag"].tolist()
        }
        
        # Add energy data if available
        if "energy_revenue" in final_df.columns:
            # Handle energy price column properly
            s_bal_data = final_df[col_energy].tolist() if col_energy and col_energy in final_df.columns else [0] * len(final_df)
            p_energy_data = final_df["p_energy"].tolist() if "p_energy" in final_df.columns else [0] * len(final_df)
            
            results["chart_data"].update({
                "S_bal": s_bal_data,
                "R_energy": final_df["energy_revenue"].tolist(),
                "p_energy": p_energy_data
            })
    
    return results


def explore_parameter_space(
    df_cap, dt_cap, col_cap, cap_unit,
    df_vol, dt_vol, col_vol_mw,
    df_pred=None, pred_cols=None, ridge_lambda=1.0, cap_model="parametric",
    portfolio_mw=1.0,
    K_cap_range=None, K_energy_range=None,
    df_energy=None, energy_pay_rule="difference", theta=0.01,
    n_points_cap=10, n_points_energy=10
) -> Dict:
    """
    Explore the parameter space by varying K_cap and K_energy systematically.
    
    Parameters
    ----------
    df_cap, dt_cap, col_cap, cap_unit : DataFrame and column specifications
        Capacity price data
    df_vol, dt_vol, col_vol_mw : DataFrame and column specifications  
        Volume data
    df_pred, pred_cols, ridge_lambda, cap_model : Optional predictor parameters
        Model configuration
    portfolio_mw : float
        Portfolio size in MW
    K_cap_range : tuple or None
        (min_K_cap, max_K_cap) range for capacity bids. If None, uses data-driven range.
    K_energy_range : tuple or None
        (min_K_energy, max_K_energy) range for energy bids. If None, uses reasonable default.
    df_energy, energy_pay_rule, theta : Energy leg parameters
        Energy configuration
    n_points_cap, n_points_energy : int
        Number of grid points for each parameter
        
    Returns
    -------
    Dict with exploration results including parameter grids and revenue surface
    """
    import numpy as np
    
    # Determine K_cap range if not provided
    if K_cap_range is None:
        cap_prices = df_cap[col_cap].dropna()
        if len(cap_prices) == 0:
            K_cap_range = (100, 500)
        else:
            price_min, price_max = cap_prices.min(), cap_prices.max()
            K_cap_range = (max(50, price_min * 0.8), min(1000, price_max * 1.2))
    
    # Determine K_energy range if not provided  
    if K_energy_range is None:
        K_energy_range = (-200, 100)  # Reasonable range for energy bids
    
    # Create parameter grids
    K_cap_grid = np.linspace(K_cap_range[0], K_cap_range[1], n_points_cap)
    K_energy_grid = np.linspace(K_energy_range[0], K_energy_range[1], n_points_energy)
    
    # Initialize results storage
    results_grid = np.zeros((n_points_cap, n_points_energy))
    capacity_revenue_grid = np.zeros((n_points_cap, n_points_energy))
    energy_revenue_grid = np.zeros((n_points_cap, n_points_energy))
    
    print(f"Exploring parameter space: K_cap {K_cap_range}, K_energy {K_energy_range}")
    print(f"Grid size: {n_points_cap} × {n_points_energy} = {n_points_cap * n_points_energy} evaluations")
    
    # Evaluate each parameter combination
    for i, K_cap in enumerate(K_cap_grid):
        for j, K_energy in enumerate(K_energy_grid):
            try:
                # Run single evaluation
                result = value_afrr_down_two_bids(
                    df_cap=df_cap, dt_cap=dt_cap, col_cap=col_cap, cap_unit=cap_unit,
                    df_vol=df_vol, dt_vol=dt_vol, col_vol_mw=col_vol_mw,
                    df_pred=df_pred, pred_cols=pred_cols, 
                    ridge_lambda=ridge_lambda, cap_model=cap_model,
                    portfolio_mw=portfolio_mw,
                    single_K_cap=K_cap, single_K_energy=K_energy,
                    df_energy=df_energy, energy_pay_rule=energy_pay_rule, theta=theta,
                    return_intervals=False  # Skip interval data for efficiency
                )
                
                results_grid[i, j] = result['total_pln_portfolio']
                capacity_revenue_grid[i, j] = result['capacity_pln_portfolio_capped'] 
                energy_revenue_grid[i, j] = result.get('energy_leg', {}).get('energy_pln_portfolio', 0)
                
            except Exception as e:
                print(f"Error at K_cap={K_cap:.1f}, K_energy={K_energy:.1f}: {e}")
                results_grid[i, j] = np.nan
                capacity_revenue_grid[i, j] = np.nan
                energy_revenue_grid[i, j] = np.nan
        
        # Progress indicator
        if (i + 1) % max(1, n_points_cap // 4) == 0:
            print(f"Completed {i + 1}/{n_points_cap} K_cap values")
    
    # Find optimal parameters
    valid_mask = ~np.isnan(results_grid)
    if np.any(valid_mask):
        max_idx = np.unravel_index(np.nanargmax(results_grid), results_grid.shape)
        optimal_K_cap = K_cap_grid[max_idx[0]]
        optimal_K_energy = K_energy_grid[max_idx[1]]
        max_revenue = results_grid[max_idx]
    else:
        optimal_K_cap = optimal_K_energy = max_revenue = np.nan
    
    # Prepare exploration results
    exploration_results = {
        'K_cap_grid': K_cap_grid.tolist(),
        'K_energy_grid': K_energy_grid.tolist(), 
        'total_revenue_grid': results_grid.tolist(),
        'capacity_revenue_grid': capacity_revenue_grid.tolist(),
        'energy_revenue_grid': energy_revenue_grid.tolist(),
        'optimal_K_cap': optimal_K_cap,
        'optimal_K_energy': optimal_K_energy,
        'max_total_revenue': max_revenue,
        'K_cap_range': K_cap_range,
        'K_energy_range': K_energy_range,
        'n_points_cap': n_points_cap,
        'n_points_energy': n_points_energy,
        'portfolio_mw': portfolio_mw,
        'energy_pay_rule': energy_pay_rule,
        'theta': theta,
        'exploration_summary': {
            'total_evaluations': n_points_cap * n_points_energy,
            'valid_evaluations': np.sum(valid_mask),
            'revenue_range_pln': [float(np.nanmin(results_grid)), float(np.nanmax(results_grid))] if np.any(valid_mask) else [0, 0]
        }
    }
    
    print(f"Exploration complete! Optimal: K_cap={optimal_K_cap:.1f}, K_energy={optimal_K_energy:.1f}")
    print(f"Max revenue: {max_revenue:,.0f} PLN")
    
    return exploration_results


if __name__ == "__main__":
    # Simple test
    print("aFRR-down two-bid valuation module loaded successfully")
    print("Main API function: value_afrr_down_two_bids()")