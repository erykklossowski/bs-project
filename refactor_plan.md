# aFRR-Down Refactoring Plan

## Architecture Overview

### Current Problems
- Single Black-Scholes call option model (incorrect for aFRR-down)
- No capacity/energy leg separation 
- Missing banded bidding (only single K)
- Incorrect budget logic
- No energy leg (short put) modeling

### Target Architecture (Two-Leg Product)

```
aFRR-Down Product
├── Capacity Leg (Pay-as-bid Option Premium)
│   ├── Acceptance Probability: p_acc,t(K) 
│   │   ├── Parametric: lognormal on P^cap_t
│   │   └── Empirical: CDF over rolling window
│   ├── Banded Bidding: (v_i, K_i) pairs
│   └── Budget Envelope: B_t = P^cap_t × min(Portfolio, Q^proc_t) × Δh
│
└── Energy Leg (Short Put on Energy Spread) - Optional
    ├── Underlying: S^bal_t (balancing energy price)
    ├── Strike: K^alt_t (alternative revenue)
    ├── Loss: (K^alt_t - S^bal_t)^+
    └── Theta Scenarios: activation intensity sweeps
```

## Core Module Refactoring

### 1. New Core Function API
```python
def value_afrr_down(
    # Capacity price & volumes (required)
    df_cap: pd.DataFrame, dt_col_cap: str, price_col_cap: str, price_unit: str,
    df_vol: pd.DataFrame, dt_col_vol: str, mw_col_vol: str,
    
    # Predictors (optional)
    df_pred: Optional[pd.DataFrame] = None, dt_col_pred: Optional[str] = None, 
    pred_cols: Optional[list[str]] = None, ridge_lambda: float = 1.0, 
    model_mode: str = "parametric",  # 'parametric' | 'empirical'
    
    # Bids
    portfolio_mw: float = 1.0,
    bids: Optional[list[tuple[float, float]]] = None,  # (v_i, K_i)
    single_K: Optional[float] = None,
    
    # Energy leg (optional)
    df_energy: Optional[pd.DataFrame] = None, dt_col_energy: Optional[str] = None, 
    energy_col: Optional[str] = None,
    df_alt: Optional[pd.DataFrame] = None, dt_col_alt: Optional[str] = None, 
    alt_col: Optional[str] = None,
    theta_list: Optional[list[float]] = None, risk_uplift: float = 0.15,
    
    # Output control
    return_intervals: bool = True
) -> dict
```

### 2. Capacity Leg Implementation
- **Acceptance Probability Models:**
  - Parametric: ln(P^cap_t) ~ N(μ_t, σ²) with ridge regression
  - Empirical: rolling CDF over historical data
- **Banded Bidding:** Support (v_i, K_i) with Σv_i = 1, K_i strictly increasing
- **Pay-as-bid Revenue:** R^cap_t = Σ v_i × K_i × p_acc,t(K_i) × min(Portfolio, Q^proc_t) × Δh
- **Budget Envelope:** B_t = P^cap_t × min(Portfolio, Q^proc_t) × Δh
- **Interval Capping:** R^cap,capped_t = min(R^cap_t, B_t)

### 3. Energy Leg Implementation  
- **Short Put Payoff:** (K^alt_t - S^bal_t)^+
- **Expected Loss:** L = E[(K^alt - S^bal)^+]
- **Theta Scenarios:** activation intensity (0.5%, 1%, 2%, 5%)
- **Premium Floor:** θ × L × (1 + risk_uplift)

### 4. Enhanced Outputs
- **Intervals CSV:** dt, P^cap, Q^proc, p_acc(K_i), R^cap, B_t, R^cap,capped, bound_flag
- **Daily/Monthly Aggregates:** Expected accepted MWh, PLN totals
- **Summary JSON:** All metrics + diagnostics + theta_sweep table
- **Chart Data:** Time series for visualization

### 5. CLI Interface
```bash
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
```

## Implementation Steps

1. **Create new afrr_valuation.py** - Clean slate implementation
2. **Capacity leg functions:**
   - `fit_parametric_model()` - lognormal with ridge regression
   - `fit_empirical_model()` - rolling CDF
   - `compute_acceptance_probability()`
   - `compute_capacity_revenue()`
   - `apply_budget_envelope()`
3. **Energy leg functions:**
   - `compute_energy_loss()`
   - `theta_sweep_analysis()`
4. **Integration functions:**
   - `value_afrr_down()` - main API
   - `create_cli()` - command line interface
5. **Update web app** to use new API
6. **Unit tests** for all key requirements

## Validation Requirements
- **Monotonicity:** p_acc(K) decreases as K increases
- **Budget binding:** Occurs in high P^cap intervals, not low
- **Banding arithmetic:** Multi-band revenue between single-K extremes
- **Unit consistency:** 15-min vs hourly normalization
- **Energy leg:** Correct short put payoff calculation

## Migration Strategy
- Keep existing web app running during development
- Create new module alongside current valuation_core.py
- Test extensively with PSE data
- Switch web app to new API once validated
- Archive old Black-Scholes implementation