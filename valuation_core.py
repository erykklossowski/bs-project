#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
aFRR-DOWN valuation core functions extracted from black-scholes_refactored.py
Contains the core Black-Scholes functionality in modular form for web app integration.
"""

import json
import math
from pathlib import Path
from typing import List, Optional, Tuple, Dict, Union

import numpy as np
import pandas as pd
from scipy.stats import norm


def read_any(path_or_df: Union[str, pd.DataFrame]) -> pd.DataFrame:
    """Read data from various file formats (CSV, Excel, JSON) or return DataFrame if already one."""
    if isinstance(path_or_df, pd.DataFrame):
        return path_or_df
    
    path = str(path_or_df)
    ext = Path(path).suffix.lower()
    if ext in (".csv", ".txt"):
        return pd.read_csv(path)
    if ext in (".xlsx", ".xls"):
        return pd.read_excel(path, sheet_name=0, engine="openpyxl")
    if ext == ".json":
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, list):
            return pd.DataFrame(raw)
        if isinstance(raw, dict):
            k = next((k for k, v in raw.items()
                      if isinstance(v, list) and v and isinstance(v[0], dict)), None)
            return pd.DataFrame(raw[k]) if k else pd.DataFrame.from_dict(raw)
    raise ValueError(f"Unsupported file type: {ext}")


def to_dt_utc(s: pd.Series) -> pd.Series:
    """Convert series to UTC datetime, handling errors gracefully."""
    s = pd.to_datetime(s, errors="coerce", utc=True)
    if s.isna().all():
        raise ValueError("Could not parse datetimes.")
    return s


def infer_interval_hours(dt_index: pd.DatetimeIndex) -> float:
    """Infer the time interval in hours from a datetime index."""
    if len(dt_index) < 2:
        return 0.25
    diffs = pd.Series(dt_index).diff().dropna().dt.total_seconds() / 3600.0
    return float(np.median(diffs)) if len(diffs) else 0.25


def align_to_quarter(df: pd.DataFrame, dt_col: str, value_cols: List[str]) -> pd.DataFrame:
    """Align an arbitrary time series (hourly or 15-min) to a 15-min grid via step hold (ffill)."""
    z = df.copy()
    z = z.dropna(subset=[dt_col])
    z[dt_col] = to_dt_utc(z[dt_col])
    z = z.sort_values(dt_col).set_index(dt_col)
    
    # Build 15-min index covering span
    full_idx = pd.date_range(
        z.index.min().floor("15min"), 
        z.index.max().ceil("15min"), 
        freq="15min", 
        tz="UTC"
    )
    z = z.reindex(full_idx).ffill()  # hold last value through the interval
    z.index.name = "dt"
    
    keep = [c for c in value_cols if c in z.columns]
    return z[keep].reset_index()


def normalize_price_units(df: pd.DataFrame, price_col: str, series_interval_h: float, price_unit: str) -> pd.Series:
    """
    Normalize price series to PLN/MW-h units.
    
    - per_mw_h: already in PLN/MW-h
    - per_mw_interval: PLN/MW per interval, divide by interval hours
    - auto: assume per_mw_h
    """
    series = df[price_col].copy()
    
    if price_unit == "per_mw_interval":
        if series_interval_h <= 0:
            series_interval_h = 0.25
        series = series / series_interval_h
    elif price_unit == "auto":
        # Assume per_mw_h
        pass
    # else per_mw_h: no conversion needed
    
    return series


def design_matrix(df: pd.DataFrame, pred_cols: List[str]) -> Tuple[np.ndarray, List[str]]:
    """Build design matrix for ridge regression with intercept."""
    if not pred_cols:
        # Intercept-only model
        X = np.ones((len(df), 1))
        used_cols = ["intercept"]
    else:
        # Intercept + predictors
        avail_cols = [c for c in pred_cols if c in df.columns]
        if not avail_cols:
            X = np.ones((len(df), 1))
            used_cols = ["intercept"]
        else:
            pred_data = df[avail_cols].fillna(0).values
            X = np.column_stack([np.ones(len(df)), pred_data])
            used_cols = ["intercept"] + avail_cols
    
    return X, used_cols


def fit_ridge(X: np.ndarray, y: np.ndarray, ridge_alpha: float = 1.0) -> Tuple[np.ndarray, float]:
    """Fit ridge regression: β = (X'X + αI)^(-1) X'y, return (coeffs, residual_std)."""
    try:
        XtX = X.T @ X
        XtX += ridge_alpha * np.eye(XtX.shape[0])
        beta = np.linalg.solve(XtX, X.T @ y)
        
        y_pred = X @ beta
        residuals = y - y_pred
        sigma = np.std(residuals, ddof=len(beta)) if len(residuals) > len(beta) else 1.0
        
        return beta, max(sigma, 0.01)
    except Exception:
        # Fallback: intercept-only with sample std
        beta = np.array([np.mean(y)])
        sigma = max(np.std(y, ddof=1), 0.01)
        return beta, sigma


def bs_call_lognormal(S: np.ndarray, K: float, mu_t: np.ndarray, sigma: float, T: np.ndarray) -> np.ndarray:
    """
    Black-Scholes call option price for lognormal underlying.
    
    Args:
        S: Current underlying prices
        K: Strike price
        mu_t: Time-varying drift
        sigma: Volatility
        T: Time to expiration (in years)
    
    Returns:
        Option values
    """
    S = np.maximum(S, 1e-6)  # Avoid log(0)
    T = np.maximum(T, 1e-6)  # Avoid division by 0
    
    d1 = (np.log(S / K) + (mu_t + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    
    call_price = S * norm.cdf(d1) - K * norm.cdf(d2)
    return np.maximum(call_price, 0)  # Ensure non-negative


def cap_column(uncapped_col: pd.Series, cap_col: pd.Series) -> pd.Series:
    """Apply element-wise cap: min(uncapped, cap)."""
    return pd.Series(
        np.minimum(uncapped_col.values, cap_col.values),
        index=uncapped_col.index
    )


def run_pipeline(
    prices_df: pd.DataFrame,
    prices_dt_col: str,
    prices_col: str, 
    prices_unit: str,
    vols_df: pd.DataFrame,
    vols_dt_col: str,
    vols_mw_col: str,
    preds_df: Optional[pd.DataFrame],
    pred_dt_col: Optional[str],
    pred_cols: List[str],
    K: float,
    portfolio_mw: float,
    ridge: float,
    mode: str
) -> Tuple[pd.DataFrame, Dict, Dict]:
    """
    Complete aFRR-down valuation pipeline.
    
    Returns:
        (intervals_df, summary_dict, chart_data_dict)
    """
    
    # 1) PRICE PROCESSING: aFRR-down capacity prices (afrr_d) → S(t)
    prices_df_clean = prices_df[[prices_dt_col, prices_col]].dropna()
    if prices_df_clean.empty:
        raise ValueError("No valid price data")
    
    # Infer price series cadence and normalize to PLN/MW-h
    prices_df_clean[prices_dt_col] = to_dt_utc(prices_df_clean[prices_dt_col])
    price_interval_h = infer_interval_hours(pd.DatetimeIndex(prices_df_clean[prices_dt_col]))
    
    # Align to 15-minute grid
    price_aligned = align_to_quarter(prices_df_clean, prices_dt_col, [prices_col])
    price_aligned["price_pln_per_mw_h"] = normalize_price_units(
        price_aligned, prices_col, price_interval_h, prices_unit
    )
    
    # 2) VOLUME PROCESSING: aFRR-down procurement (zmb_afrrd) → actual MW
    vols_df_clean = vols_df[[vols_dt_col, vols_mw_col]].dropna()
    if vols_df_clean.empty:
        raise ValueError("No valid volume data")
    
    vol_aligned = align_to_quarter(vols_df_clean, vols_dt_col, [vols_mw_col])
    vol_aligned["mw_procured"] = vol_aligned[vols_mw_col].clip(lower=0)
    
    # 3) PREDICTOR PROCESSING: CEN/COR/SK → μₜ modeling
    if preds_df is not None and pred_dt_col and pred_cols:
        pred_aligned = align_to_quarter(preds_df, pred_dt_col, pred_cols)
    else:
        pred_aligned = None
    
    # 4) TIME ALIGNMENT: Merge all datasets to common 15-minute UTC grid
    df = price_aligned[["dt", "price_pln_per_mw_h"]].copy()
    df = df.merge(vol_aligned[["dt", "mw_procured"]], on="dt", how="inner")
    
    if pred_aligned is not None:
        df = df.merge(pred_aligned, on="dt", how="left")
    
    df = df.sort_values("dt").reset_index(drop=True)
    if df.empty:
        raise ValueError("No overlapping data after time alignment")
    
    # Add time features
    df["Hours_in_interval"] = 0.25  # 15-minute intervals
    df["T_years"] = 0.25 / (365.25 * 24)  # Time to expiration for each interval
    
    # 5) BUDGET CONSTRUCTION: price_afrrd × mw_procured × Δh
    df["budget_pln_interval"] = (
        df["price_pln_per_mw_h"].clip(lower=0) *
        df["mw_procured"].clip(lower=0) *
        df["Hours_in_interval"]
    )
    
    # 6) PREDICTOR MODELING: μₜ estimation via Ridge regression
    if pred_aligned is not None and not df[pred_cols].dropna().empty:
        # Use log(price) for drift modeling (financial convention)
        log_prices = np.log(df["price_pln_per_mw_h"].clip(lower=1))
        valid_mask = np.isfinite(log_prices) & df[pred_cols].notna().all(axis=1)
        
        if valid_mask.sum() > 10:
            X, used_cols = design_matrix(df[valid_mask], pred_cols)
            beta_mu, sigma_residual = fit_ridge(X, log_prices[valid_mask], ridge)
            
            # Apply fitted model to all data
            X_all, _ = design_matrix(df, pred_cols)
            mu_t_series = X_all @ beta_mu
        else:
            # Fallback to intercept-only
            mu_t_series = np.full(len(df), np.mean(log_prices[np.isfinite(log_prices)]))
            sigma_residual = np.std(log_prices[np.isfinite(log_prices)])
            beta_mu = np.array([mu_t_series[0]])
            used_cols = ["intercept"]
    else:
        # No predictors: constant drift
        log_prices = np.log(df["price_pln_per_mw_h"].clip(lower=1))
        mu_t_series = np.full(len(df), np.mean(log_prices[np.isfinite(log_prices)]))
        sigma_residual = max(np.std(log_prices[np.isfinite(log_prices)]), 0.01)
        beta_mu = np.array([mu_t_series[0]])
        used_cols = ["intercept"]
    
    # 7) VALUATION
    S_values = df["price_pln_per_mw_h"].values
    mu_values = mu_t_series
    T_values = df["T_years"].values
    
    results = {"mode": mode, "K": K, "portfolio_mw": portfolio_mw}
    
    if mode in ["exante", "both"]:
        # Ex-ante Black-Scholes option pricing
        df["exante_option_value_per_mw"] = bs_call_lognormal(
            S_values, K, mu_values, sigma_residual, T_values
        )
        df["exante_p_accept"] = norm.cdf(
            (np.log(S_values / K) + (mu_values + 0.5 * sigma_residual**2) * T_values) / 
            (sigma_residual * np.sqrt(T_values))
        )
        
        # Portfolio calculations
        df["exante_pln_per_mw_interval"] = df["exante_option_value_per_mw"] * df["Hours_in_interval"]
        df["exante_pln_portfolio_baseline"] = df["exante_pln_per_mw_interval"] * portfolio_mw
        
        # Budget-capped portfolio
        portfolio_cap = df["budget_pln_interval"] * np.minimum(1.0, portfolio_mw / df["mw_procured"].clip(lower=1))
        df["exante_pln_portfolio_capped"] = cap_column(df["exante_pln_portfolio_baseline"], portfolio_cap)
        
        # Summary statistics
        results.update({
            "exante_sigma": sigma_residual,
            "exante_beta": beta_mu.tolist(),
            "exante_predictor_cols": used_cols,
            "exante_expected_MWh": (df["exante_p_accept"] * df["Hours_in_interval"]).sum(),
            "exante_total_PLN_perMW": df["exante_pln_per_mw_interval"].sum(),
            "exante_portfolio_baseline_total_PLN": df["exante_pln_portfolio_baseline"].sum(),
            "exante_portfolio_capped_total_PLN": df["exante_pln_portfolio_capped"].sum(),
            "exante_share_bound_intervals_pct": 100.0 * (df["exante_pln_portfolio_capped"] < df["exante_pln_portfolio_baseline"]).mean()
        })
    
    if mode in ["expost", "both"]:
        # Ex-post realized payoff
        df["expost_realized_payoff_per_mw"] = np.maximum(df["price_pln_per_mw_h"] - K, 0)
        df["expost_p_accept"] = (df["price_pln_per_mw_h"] >= K).astype(float)
        
        # Portfolio calculations
        df["expost_pln_per_mw_interval"] = df["expost_realized_payoff_per_mw"] * df["Hours_in_interval"]
        df["expost_pln_portfolio_baseline"] = df["expost_pln_per_mw_interval"] * portfolio_mw
        
        # Budget-capped portfolio
        portfolio_cap = df["budget_pln_interval"] * np.minimum(1.0, portfolio_mw / df["mw_procured"].clip(lower=1))
        df["expost_pln_portfolio_capped"] = cap_column(df["expost_pln_portfolio_baseline"], portfolio_cap)
        
        # Summary statistics
        results.update({
            "expost_expected_MWh": (df["expost_p_accept"] * df["Hours_in_interval"]).sum(),
            "expost_total_PLN_perMW": df["expost_pln_per_mw_interval"].sum(),
            "expost_portfolio_baseline_total_PLN": df["expost_pln_portfolio_baseline"].sum(),
            "expost_portfolio_capped_total_PLN": df["expost_pln_portfolio_capped"].sum(),
            "expost_share_bound_intervals_pct": 100.0 * (df["expost_pln_portfolio_capped"] < df["expost_pln_portfolio_baseline"]).mean()
        })
    
    # 8) CHART DATA for web display
    chart_data = {
        "dt": df["dt"].dt.strftime("%Y-%m-%d %H:%M:%S").tolist(),
        "price_pln_per_mw_h": df["price_pln_per_mw_h"].tolist(),
        "strike_line": [K] * len(df),
        "mw_procured": df["mw_procured"].tolist(),
        "budget_pln_interval": df["budget_pln_interval"].tolist()
    }
    
    # Add acceptance probability based on mode
    if mode == "exante":
        chart_data["p_accept"] = df["exante_p_accept"].tolist()
        chart_data["pln_portfolio_capped"] = df["exante_pln_portfolio_capped"].tolist()
    elif mode == "expost":
        chart_data["p_accept"] = df["expost_p_accept"].tolist()
        chart_data["pln_portfolio_capped"] = df["expost_pln_portfolio_capped"].tolist()
    elif mode == "both":
        # For 'both' mode, show ex-ante by default in charts
        chart_data["p_accept"] = df["exante_p_accept"].tolist()
        chart_data["pln_portfolio_capped"] = df["exante_pln_portfolio_capped"].tolist()
    
    return df, results, chart_data