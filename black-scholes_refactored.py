#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
aFRR-DOWN valuation (capacity-only) with:
 - explicit price-×-volume budget per interval,
 - ex-ante Black–Scholes-style option valuation,
 - optional ex-post realized payoff,
 - robust alignment of mixed granularities (hourly vs 15-min).

Examples:
  # Minimal: ex-ante with aFRR-down prices + procured MW (hourly or 15min), no predictors
  python black-scholes_refactored.py \
      --prices CENY_AFRRD.xlsx --prices-dt-col ts --prices-col price_pln_per_mw_h --prices-unit per_mw_h \
      --vols VOLUMES_AFRRD.xlsx --vols-dt-col ts --vols-mw-col mw_procured \
      --K 400 --portfolio-mw 50 --out-prefix run_K400

  # With predictors (CEN,COR,SK...) to model μ_t
  python black-scholes_refactored.py \
      --prices prices.xlsx --prices-dt-col dt --prices-col aFRRd_price --prices-unit auto \
      --vols vols.xlsx --vols-dt-col dt --vols-mw-col aFRRd_mw \
      --predictors feats.csv --pred-dt-col dt \
      --pred-cols CEN,COR,CSDAC,SK \
      --K 400 --portfolio-mw 50 --ridge 1.0 --mode both --out-prefix wind_afrrd_K400

Dependencies: pandas numpy scipy matplotlib openpyxl
"""

import argparse
import json
import math
import sys
from pathlib import Path
from typing import List, Optional, Tuple, Dict, Union

import numpy as np
import pandas as pd
from scipy.stats import norm
import matplotlib.pyplot as plt


# ---------------------------- Utilities ----------------------------

def read_any(path: str) -> pd.DataFrame:
    """Read data from various file formats (CSV, Excel, JSON)."""
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
    
    # Build 15-min index covering span (using 'min' instead of deprecated 'T')
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
    UNITS & CADENCE POLICY:
    
    All prices normalized to PLN/MW-h (Polish złoty per MW-hour) for consistency.
    All time series aligned to 15-minute intervals (0.25 hours) via forward-fill.
    
    Unit conversion rules:
    - 'per_mw_h': Already PLN/MW-h → return as-is (standard for aFRR-down capacity pricing)
    - 'per_mw_interval': PLN/MW per interval → divide by interval_hours (Δh = 0.25h for 15-min)
    - 'auto': Assumes per_mw_h (default for PSE afrr_d data)
    
    Granularity handling:
    - Hourly data: Expanded to four 15-minute intervals via forward-fill
    - 15-minute data: Used directly (native PSE market cadence)
    - Other intervals: Aligned to 15-minute grid via temporal resampling
    
    Budget implications: price_pln_per_mw_h × mw_procured × 0.25h = interval budget
    """
    s = pd.to_numeric(df[price_col], errors="coerce")
    if price_unit == "per_mw_h":
        return s
    elif price_unit == "per_mw_interval":
        # Convert per-interval pricing to per-hour: divide by interval duration
        return s / max(series_interval_h, 1e-9)
    elif price_unit == "auto":
        # Default assumption: data is already in PLN/MW-h (PSE convention)
        return s
    else:
        raise ValueError("--prices-unit must be one of: auto, per_mw_h, per_mw_interval")


def design_matrix(df: pd.DataFrame, pred_cols: List[str], ridge: float) -> Tuple[np.ndarray, np.ndarray]:
    """
    Create design matrix for μₜ drift modeling via ridge regression.
    
    CRITICAL DISTINCTION:
    - y (target): ln(price_pln_per_mw_h) where price_pln_per_mw_h = afrr_d (underlying price)
    - X (predictors): CEN, COR, SK, CSDAC etc. (market predictors for drift modeling)
    
    The aFRR-down price (afrr_d) is the UNDERLYING price S(t) in Black-Scholes.
    The predictors (CEN, COR, SK) are used to model μₜ time-varying drift only.
    
    Returns: (X_matrix_with_intercept, y_ln_underlying_price)
    Usage: Fed into fit_ridge() → μₜ coefficients → bs_call_lognormal()
    """
    # TARGET: Natural log of UNDERLYING aFRR-down price (afrr_d) 
    y = np.log(df["price_pln_per_mw_h"].clip(lower=1e-9))
    
    # PREDICTORS: Market variables for μₜ drift estimation (CEN, COR, SK, CSDAC)
    X_cols = [c for c in pred_cols if c in df.columns]
    X = df[X_cols].astype(float).values if X_cols else np.zeros((len(df), 0))
    
    # Add intercept term for regression
    X = np.c_[np.ones(len(df)), X]
    
    # Ridge regularization parameter
    if ridge < 0:
        ridge = 0.0
    return X, y


def fit_ridge(X: np.ndarray, y: np.ndarray, lam: float) -> Tuple[np.ndarray, float]:
    """Closed-form ridge: beta = (X'X + λI)^-1 X'y; returns (beta, sigma)."""
    n, p = X.shape
    A = X.T @ X + lam * np.eye(p)
    b = X.T @ y
    beta = np.linalg.solve(A, b)
    resid = y - X @ beta
    sigma = float(np.std(resid, ddof=min(p, n-1)))
    return beta, sigma


def bs_call_lognormal(mu: np.ndarray, sigma: float, K: float) -> np.ndarray:
    """
    Black-Scholes call option valuation: E[(S-K)^+] for lognormal underlying S.
    
    UNDERLYING PRICE CLARIFICATION:
    - S(t): aFRR-down capacity marginal price (afrr_d field from PSE cmbu-tu endpoint)
    - μₜ: Time-varying drift estimated from market predictors (CEN, COR, SK, CSDAC)
    - σ: Volatility parameter estimated from aFRR-down price residuals
    - K: Strike price (user-specified, e.g. 400 PLN/MW-h)
    
    Formula: E[(S-K)^+] where S ~ lognormal(μₜ, σ²)
    Returns: Expected payoff per MW-h for each time interval
    """
    K = max(K, 1e-12)
    if sigma <= 1e-12:
        # Degenerate case: no volatility, direct calculation
        s0 = np.exp(mu)
        return np.maximum(s0 - K, 0.0)
    
    # Standard Black-Scholes formula for call options
    d1 = (mu + sigma**2 - math.log(K)) / sigma
    d2 = d1 - sigma
    term1 = np.exp(mu + 0.5 * sigma**2) * norm.cdf(d1)  # S × N(d1)
    term2 = K * norm.cdf(d2)                            # K × N(d2)
    return np.maximum(term1 - term2, 0.0)


def realized_payoff(price: np.ndarray, K: float) -> np.ndarray:
    """Calculate realized payoff: max(price - K, 0)."""
    return np.maximum(price - K, 0.0)


def daily_monthly(df: pd.DataFrame, cols: List[str]) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Generate daily and monthly summaries."""
    d = df.copy()
    d["date"] = d["dt"].dt.date
    m = d.copy()
    # Convert to period without timezone to avoid warning
    m["month"] = d["dt"].dt.tz_localize(None).dt.to_period("M").astype(str)
    
    daily = d.groupby("date", as_index=False)[cols].sum(numeric_only=True)
    monthly = m.groupby("month", as_index=False)[cols].sum(numeric_only=True)
    return daily, monthly


def plot_series(df: pd.DataFrame, col: str, title: str, out_png: str):
    """Plot time series and save to PNG."""
    if df.empty or df[col].isna().all():
        print(f"[warn] nothing to plot for {col}")
        return
    
    plt.figure(figsize=(12, 4))
    plt.plot(df["dt"], df[col], linewidth=1.0)
    plt.title(title)
    plt.grid(True, linestyle=":", linewidth=0.5)
    plt.xlabel("UTC time")
    plt.ylabel(col)
    plt.tight_layout()
    plt.savefig(out_png, dpi=140)
    plt.close()
    print(f"[ok] saved {out_png}")


def cap_column(df: pd.DataFrame, src_col: str, out_col: str, portfolio_mw: float, cap_share_pln: str) -> Tuple[float, float, float, float]:
    """Apply budget cap to a column and return statistics."""
    base = df[src_col] * portfolio_mw
    scale = np.where(
        base > df[cap_share_pln], 
        df[cap_share_pln] / np.maximum(base, 1e-9), 
        1.0
    )
    df[out_col] = base * scale
    return (
        float(base.sum()), 
        float(df[out_col].sum()), 
        float((scale < 0.999).mean() * 100.0), 
        float(np.mean(scale))
    )


# ---------------------------- Main pipeline ----------------------------

def run_pipeline(
    prices_path: str, 
    prices_dt_col: str, 
    prices_col: str, 
    prices_unit: str,
    vols_path: str, 
    vols_dt_col: str, 
    vols_mw_col: str,
    predictors_path: Optional[str], 
    pred_dt_col: Optional[str], 
    pred_cols: List[str],
    K: float, 
    portfolio_mw: float, 
    ridge: float, 
    mode: str, 
    out_prefix: str
) -> None:
    """Main pipeline for aFRR-DOWN valuation."""
    
    # 1) Read & normalize price series
    print("[info] Reading price data...")
    p_raw = read_any(prices_path)
    if prices_dt_col not in p_raw.columns or prices_col not in p_raw.columns:
        raise ValueError("prices dt/price columns not found.")
    
    p_raw["dt"] = to_dt_utc(p_raw[prices_dt_col])
    price_interval_h_raw = infer_interval_hours(p_raw["dt"].dropna().sort_values().unique())
    p_raw["price_pln"] = pd.to_numeric(p_raw[prices_col], errors="coerce")

    # Align to 15-min and normalize units to PLN/MW-h
    p_al = align_to_quarter(p_raw[["dt", "price_pln"]], "dt", ["price_pln"])
    p_al_int_h = infer_interval_hours(p_raw["dt"])  # original cadence (for per-interval unit)
    p_al["price_pln_per_mw_h"] = normalize_price_units(
        p_al.rename(columns={"price_pln": prices_col}),
        price_col=prices_col,
        series_interval_h=p_al_int_h,
        price_unit=prices_unit
    )
    p_al = p_al.dropna(subset=["price_pln_per_mw_h"])

    # 2) Read & normalize aFRR-down procured MW
    print("[info] Reading volume data...")
    v_raw = read_any(vols_path)
    if vols_dt_col not in v_raw.columns or vols_mw_col not in v_raw.columns:
        raise ValueError("vols dt/mw columns not found.")
    
    v_raw["dt"] = to_dt_utc(v_raw[vols_dt_col])
    v_raw["mw_procured"] = pd.to_numeric(v_raw[vols_mw_col], errors="coerce")
    v_al = align_to_quarter(v_raw[["dt", "mw_procured"]], "dt", ["mw_procured"])

    # 3) Predictors (optional)
    if predictors_path:
        print("[info] Reading predictor data...")
        w_raw = read_any(predictors_path)
        if pred_dt_col not in w_raw.columns:
            raise ValueError("predictors dt column not found.")
        
        w_raw["dt"] = to_dt_utc(w_raw[pred_dt_col])
        keep_cols = [c for c in pred_cols if c in w_raw.columns]
        w_al = align_to_quarter(w_raw[["dt"] + keep_cols], "dt", keep_cols)
    else:
        w_al = pd.DataFrame({"dt": p_al["dt"]})

    # 4) Merge all on 15-min grid
    print("[info] Aligning and merging data...")
    df = p_al.merge(v_al, on="dt", how="outer").merge(w_al, on="dt", how="outer")
    df = df.sort_values("dt").reset_index(drop=True)
    
    # Forward/back-fill predictors, hold price & MW at last known value (already ffilled by align)
    for c in pred_cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
            df[c] = df[c].ffill().bfill()

    # 5) Hours in interval (now 0.25 by construction)
    df["Hours_in_interval"] = 0.25

    # 6) EXPLICIT BUDGET CONSTRUCTION: price_afrrd × mw_procured_afrrd × Δh
    # 
    # CRITICAL: This is the per-interval aFRR-down spend using ACTUAL data:
    # - price_pln_per_mw_h: aFRR-down marginal price (afrr_d) in PLN/MW-h
    # - mw_procured: Actual aFRR-down procurement (zmb_afrrd) in MW
    # - Hours_in_interval: 0.25 hours (15-minute market intervals)
    #
    # Portfolio cap formula: portfolio_revenues ≤ fair_share × budget_pln_interval
    # where fair_share = min(1, portfolio_MW / mw_procured) per interval
    #
    df["budget_pln_interval"] = (
        df["price_pln_per_mw_h"].clip(lower=0) *  # aFRR-down price (PLN/MW-h)
        df["mw_procured"].clip(lower=0) *         # Actual procurement (MW)
        df["Hours_in_interval"]                   # 15-min interval (0.25h)
    )

    # 7) Ex-ante Black–Scholes (μ_t from predictors, σ global)
    results = {}
    if mode in ("exante", "both"):
        print("[info] Computing ex-ante valuation...")
        X, y_lnP = design_matrix(df, pred_cols, ridge)
        beta, sigma = fit_ridge(X, y_lnP, lam=ridge)
        mu_t = X @ beta
        df["p_accept_exante"] = 1.0 - norm.cdf((math.log(K) - mu_t) / max(sigma, 1e-12))
        call_per_mwh = bs_call_lognormal(mu_t, sigma, K)
        df["val_perMW_h_exante"] = call_per_mwh
        df["val_perMW_interval_exante"] = df["val_perMW_h_exante"] * df["Hours_in_interval"]
        results.update({
            "exante_sigma": float(sigma),
            "exante_beta": {f"b{i}": float(b) for i, b in enumerate(beta)},
            "exante_expected_MWh": float((df["p_accept_exante"] * df["Hours_in_interval"]).sum()),
            "exante_total_PLN_perMW": float(df["val_perMW_interval_exante"].sum())
        })

    # 8) Ex-post (realized)
    if mode in ("expost", "both"):
        print("[info] Computing ex-post valuation...")
        price = df["price_pln_per_mw_h"].to_numpy()
        payoff = realized_payoff(price, K)
        df["p_accept_expost"] = (price >= K).astype(float)
        df["val_perMW_h_expost"] = payoff
        df["val_perMW_interval_expost"] = df["val_perMW_h_expost"] * df["Hours_in_interval"]
        results.update({
            "expost_expected_MWh": float((df["p_accept_expost"] * df["Hours_in_interval"]).sum()),
            "expost_total_PLN_perMW": float(df["val_perMW_interval_expost"].sum())
        })

    # 9) Apply budget cap per interval to portfolio PLN (strict, interval-wise)
    # Your fair cap share at t: budget_t * min(1, portfolio_MW / mw_procured_t)
    df["cap_share_pln"] = np.where(
        df["mw_procured"] > 0,
        df["budget_pln_interval"] * np.minimum(1.0, portfolio_mw / df["mw_procured"]),
        0.0
    )

    if "val_perMW_interval_exante" in df.columns:
        b_total, c_total, pct_bound, avg_scale = cap_column(
            df, "val_perMW_interval_exante", "portfolio_pln_interval_exante_capped", 
            portfolio_mw, "cap_share_pln"
        )
        results.update({
            "exante_portfolio_baseline_total_PLN": b_total,
            "exante_portfolio_capped_total_PLN": c_total,
            "exante_share_bound_intervals_%": pct_bound,
            "exante_avg_scale_factor": avg_scale
        })

    if "val_perMW_interval_expost" in df.columns:
        b_total, c_total, pct_bound, avg_scale = cap_column(
            df, "val_perMW_interval_expost", "portfolio_pln_interval_expost_capped", 
            portfolio_mw, "cap_share_pln"
        )
        results.update({
            "expost_portfolio_baseline_total_PLN": b_total,
            "expost_portfolio_capped_total_PLN": c_total,
            "expost_share_bound_intervals_%": pct_bound,
            "expost_avg_scale_factor": avg_scale
        })

    # 10) Summaries & exports
    print("[info] Saving results...")
    out_prefix = out_prefix or "affr_down"
    Path(".").mkdir(parents=True, exist_ok=True)
    
    # Per-interval CSV
    df_out = df[[
        "dt", "Hours_in_interval", "price_pln_per_mw_h", "mw_procured", "budget_pln_interval",
        *([ "p_accept_exante", "val_perMW_h_exante", "val_perMW_interval_exante", "portfolio_pln_interval_exante_capped" ] if "val_perMW_interval_exante" in df.columns else []),
        *([ "p_accept_expost", "val_perMW_h_expost", "val_perMW_interval_expost", "portfolio_pln_interval_expost_capped" ] if "val_perMW_interval_expost" in df.columns else []),
        "cap_share_pln"
    ]]
    df_out.to_csv(f"{out_prefix}_intervals.csv", index=False)

    # Daily / monthly
    sum_cols = []
    if "val_perMW_interval_exante" in df.columns:
        sum_cols += ["val_perMW_interval_exante", "portfolio_pln_interval_exante_capped"]
    if "val_perMW_interval_expost" in df.columns:
        sum_cols += ["val_perMW_interval_expost", "portfolio_pln_interval_expost_capped"]
    sum_cols += ["budget_pln_interval"]
    daily, monthly = daily_monthly(df, sum_cols)
    daily.to_csv(f"{out_prefix}_daily.csv", index=False)
    monthly.to_csv(f"{out_prefix}_monthly.csv", index=False)

    # JSON summary
    with open(f"{out_prefix}_summary.json", "w", encoding="utf-8") as f:
        json.dump({
            "K": K,
            "portfolio_mw": portfolio_mw,
            "price_series_original_interval_h": price_interval_h_raw,
            "mode": mode,
            **results
        }, f, indent=2, ensure_ascii=False)

    # Plots
    if "p_accept_exante" in df.columns:
        plot_series(df, "p_accept_exante",
                    f"Ex-ante acceptance probability (K={int(K)})", f"{out_prefix}_p_accept_exante.png")
    plot_series(df, "price_pln_per_mw_h",
                "aFRR-down capacity price [PLN/MW-h]", f"{out_prefix}_price.png")
    plot_series(df, "mw_procured", "aFRR-down procured MW", f"{out_prefix}_mw_procured.png")

    print("[ok] saved:",
          f"{out_prefix}_intervals.csv, {out_prefix}_daily.csv, {out_prefix}_monthly.csv, {out_prefix}_summary.json")


def main():
    """Main entry point with argument parsing."""
    ap = argparse.ArgumentParser(description="aFRR-DOWN valuation (capacity-only) with explicit budget & 15-min alignment")
    
    # Prices
    ap.add_argument("--prices", required=True, help="Path to aFRR-down capacity price file (CSV/XLSX/JSON)")
    ap.add_argument("--prices-dt-col", required=True, help="Datetime column in prices file")
    ap.add_argument("--prices-col", required=True, help="Price column in prices file")
    ap.add_argument("--prices-unit", default="auto", choices=["auto", "per_mw_h", "per_mw_interval"],
                    help="Units of price column; auto=assume PLN/MW-h (override if per-interval)")
    
    # Volumes
    ap.add_argument("--vols", required=True, help="Path to aFRR-down procured MW file (CSV/XLSX/JSON)")
    ap.add_argument("--vols-dt-col", required=True, help="Datetime column in volumes file")
    ap.add_argument("--vols-mw-col", required=True, help="MW column in volumes file")
    
    # Predictors (optional)
    ap.add_argument("--predictors", default=None, help="Path to predictors file (CEN,COR,CSDAC,SK...)")
    ap.add_argument("--pred-dt-col", default=None, help="Datetime column in predictors file")
    ap.add_argument("--pred-cols", default="", help="Comma-separated predictor columns to use")
    
    # Valuation
    ap.add_argument("--K", type=float, default=400.0, help="Strike price [PLN/MW-h]")
    ap.add_argument("--portfolio-mw", type=float, default=1.0, help="Portfolio size to cap against budget [MW]")
    ap.add_argument("--ridge", type=float, default=1.0, help="Ridge λ for μ_t fit (0=no ridge)")
    ap.add_argument("--mode", default="exante", choices=["exante", "expost", "both"], help="Valuation mode")
    ap.add_argument("--out-prefix", default="affr_down", help="Output prefix")
    
    args = ap.parse_args()

    pred_cols = [c.strip() for c in args.pred_cols.split(",") if c.strip()]
    if (args.predictors is None) ^ (args.pred_dt_col is None):
        print("[warn] predictors path or pred-dt-col missing; ignoring predictors.")
        args.predictors, args.pred_dt_col, pred_cols = None, None, []

    try:
        run_pipeline(
            prices_path=args.prices, 
            prices_dt_col=args.prices_dt_col, 
            prices_col=args.prices_col, 
            prices_unit=args.prices_unit,
            vols_path=args.vols, 
            vols_dt_col=args.vols_dt_col, 
            vols_mw_col=args.vols_mw_col,
            predictors_path=args.predictors, 
            pred_dt_col=args.pred_dt_col, 
            pred_cols=pred_cols,
            K=args.K, 
            portfolio_mw=args.portfolio_mw, 
            ridge=args.ridge, 
            mode=args.mode, 
            out_prefix=args.out_prefix
        )
    except Exception as e:
        print(f"[error] Pipeline failed: {e}")
        raise


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[error] {e}")
        sys.exit(1)
