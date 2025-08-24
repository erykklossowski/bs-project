# aFRR-Down Valuation (Capacity = Option Premium; Energy = Short-Put)

## 0) Goal

Implement a Python module + simple CLI that values **aFRR-down** for a BSP over a chosen date range using **historical market data**. Treat the product correctly:

- **Capacity component** = **option premium** discovered in the auction. In pay-as-bid, the BSP is paid **their bid** when their band is selected; there is **no “clearing-minus-K”** surplus in invoices.
- **Energy/activation component** = BSP is **short a put** on the energy spread. If activated down, the loss per activated MWh is $(K_{\text{alt}} - S_{\text{bal}})^+$ where:
  - $S_{\text{bal}}$ = balancing **energy** price (down),
  - $K_{\text{alt}}$ = BSP’s **alternative** revenue (e.g., DA price / internal hedge).

We need **ex-ante** acceptance probabilities for the capacity leg and an **activation risk band** for the energy leg. Provide robust **unit handling** (15-min vs hourly), **banded bids**, and a **budget envelope** that constrains expected pay-as-bid revenue interval-by-interval.

------

## 1) Data & Units

- All time stamps → **UTC**, aligned to **15-minute** grid.
- Prices to **PLN/MW-h**:
  - If input is **PLN/MW per 15-min interval**, divide by **0.25 h**.
- Interval hours: $\Delta h = 0.25$.

### Required series

1. **Capacity price (aFRR-down):** $P^{cap}_t$ (PLN/MW-h) — the auction premium per MW-h.
2. **Procured aFRR-down MW:** $Q^{proc}_t$ (MW) — procured volume per interval (for envelope and market share).
3. **Predictors (optional)** for ex-ante modeling of capacity price level (to estimate selection probability): e.g., CEN, COR, CSDAC, SK, etc.
4. *(Optional)* **Balancing energy price (down):** $S^{bal}_t$ (PLN/MWh).
5. *(Optional)* **Alternative price:** $K^{alt}_t$ (PLN/MWh), e.g., day-ahead (CSDAC) or internal value.

*(If available via PSE API, include a fetcher; otherwise accept CSV/XLSX/JSON files.)*

------

## 2) Economics (two separate legs)

### 2.1 Capacity leg = **option premium** (pay-as-bid)

**Bids:**

- Allow either **single flat bid** $K$ (PLN/MW-h) or **banded bids**: fractions $v_i$ (sum to 1) with increasing bids $K_i$.

**Acceptance probability (ex-ante):**

- Model the distribution of $P^{cap}_t$. Two interchangeable modes:
  1. **Parametric (lognormal on price)**
      $\ln P^{cap}_t \sim \mathcal{N}(\mu_t,\sigma^2)$ with $\mu_t$ from ridge regression on predictors and $\sigma$ from residuals.
      $p_{\text{acc},t}(K) = \Pr(P^{cap}_t \ge K) = 1 - \Phi\!\left(\frac{\ln K - \mu_t}{\sigma}\right)$.
  2. **Empirical CDF** over a rolling window (fallback):
      $p_{\text{acc},t}(K) = \frac{\#\{P^{cap}\ge K\}}{\#\text{obs}}$.

**Expected pay-as-bid revenue (per interval):**

- **Single bid** $K$:
   $R^{cap}_t = K \cdot p_{\text{acc},t}(K) \cdot \min(\text{PortfolioMW}, Q^{proc}_t)\cdot \Delta h$.
- **Banded bids** $(v_i,K_i)$:
   $R^{cap}_t = \sum_i v_i K_i p_{\text{acc},t}(K_i)\cdot \min(\text{PortfolioMW}, Q^{proc}_t)\cdot \Delta h$.

**Budget envelope (interval-level):**

- **Your fair-share spend ceiling** in interval $t$:
   $B_t = P^{cap}_t \cdot \min(\text{PortfolioMW}, Q^{proc}_t)\cdot \Delta h.$
- **Cap** per interval: $R^{cap,capped}_t = \min(R^{cap}_t,\, B_t)$.
   **Bound flag**: $1$ if $R^{cap}_t > B_t$, else $0$.

> Intuition check: For **lower K**, even though $p_{\text{acc}}(K)$ rises, $K\cdot p_{\text{acc}}$ typically **declines** → **less** chance to hit the budget cap — binds move to **high-price** intervals, not low.

**Capacity outputs (aggregate):**

- Per-MW & portfolio totals (baseline and capped).
- Share of bound intervals (%).
- Daily/monthly aggregates.
- Diagnostics: $\sigma$, $\beta$ on predictors, distribution quantiles of $P^{cap}$, share $P^{cap}\ge K$.

### 2.2 Energy/activation leg = **short put** on energy spread (optional, sensitivity)

- Underlying: $S^{bal}_t$ (balancing energy price, down).
- Strike: $K^{alt}_t$ (alternative price/revenue).
- **Loss per activated MWh**: $(K^{alt}_t - S^{bal}_t)^+$.
- **Expected loss per activated MWh**: $L = \mathbb{E}[(K^{alt}-S^{bal})^+]$ (use full-period mean or rolling).
- Activation intensity scenario $\theta$ (activated MWh per MW-h, e.g., 0.5%/1%/2%/5%).
   **Premium floor per MW-h**: $\theta \cdot L$.
   Optionally add a **risk uplift** (e.g., +15%).

*(This is not subtracted from capacity value automatically; report as a separate sensitivity band.)*

------

## 3) Algorithmic Steps

1. **Load & align** all time series to 15-min UTC; infer $\Delta h$.
2. **Normalize units** (prices to PLN/MW-h; volumes in MW).
3. **Merge** on `dt`.
4. **Capacity leg (ex-ante):**
   - Fit $\mu_t,\sigma$ (parametric) or build empirical CDF.
   - Compute $p_{\text{acc},t}(K)$ for single or banded bids.
   - Compute **expected pay-as-bid** $R^{cap}_t$.
   - Compute envelope $B_t$; cap and flag bounds.
5. **Activation leg (optional):**
   - Compute $L$ from $(K^{alt}-S^{bal})^+$.
   - Produce $\theta$-sweep table for **premium floor**.
6. **Summaries**: totals (per-MW, portfolio), bound %, diagnostics; export interval/daily/monthly CSVs + JSON summary.
7. **Charts** (for quick inspection):
   - $P^{cap}$ vs bid $K$ (+ top decile band),
   - $p_{\text{acc}}(K)$ over time,
   - Procured MW and spend envelope vs expected pay-as-bid (capped),
   - (Optional) rolling $L$ and premium floor for a chosen $\theta$.

------

## 4) Interfaces

### 4.1 Core function (Python)

```
def value_afrr_down(
    # Capacity price & volumes (required)
    df_cap: pd.DataFrame, dt_col_cap: str, price_col_cap: str, price_unit: str,  # 'per_mw_h' | 'per_mw_interval' | 'auto'
    df_vol: pd.DataFrame, dt_col_vol: str, mw_col_vol: str,

    # Predictors (optional for parametric μ_t)
    df_pred: Optional[pd.DataFrame] = None, dt_col_pred: Optional[str] = None, pred_cols: Optional[list[str]] = None,
    ridge_lambda: float = 1.0, model_mode: str = "parametric",  # 'parametric' | 'empirical'

    # Bids
    portfolio_mw: float = 1.0,
    bids: Optional[list[tuple[float, float]]] = None,  # list of (v_i, K_i). If None, use single K
    single_K: Optional[float] = None,

    # Activation (optional)
    df_energy: Optional[pd.DataFrame] = None, dt_col_energy: Optional[str] = None, energy_col: Optional[str] = None,
    df_alt: Optional[pd.DataFrame] = None, dt_col_alt: Optional[str] = None, alt_col: Optional[str] = None,
    theta_list: Optional[list[float]] = None, risk_uplift: float = 0.15,

    # Output control
    return_intervals: bool = True
) -> dict:
    """
    Returns a dict with:
      - capacity_perMW_baseline_PLN, capacity_perMW_capped_PLN
      - portfolio_baseline_PLN, portfolio_capped_PLN
      - bound_intervals_pct
      - exante_sigma, exante_beta (if parametric)
      - acceptance_stats (per bid)
      - activation_L_pln_per_MWh (if provided) and theta_sweep table
      - charts payload (compact arrays) if return_intervals
    """
```

### 4.2 CLI

```
python affr_down.py \
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

Support `--bands v1:K1,v2:K2,...` as an alternative to `--single-K`.

------

## 5) Correct “Budget” Logic (must have)

Per interval $t$:

- $B_t = P^{cap}_t \cdot \min(\text{PortfolioMW}, Q^{proc}_t)\cdot \Delta h$.
- **Single K**: $R^{cap}_t = K \cdot p_{\text{acc},t}(K)\cdot \min(\text{PortfolioMW}, Q^{proc}_t)\cdot \Delta h$.
- **Bands**: $R^{cap}_t = \sum_i v_i K_i p_{\text{acc},t}(K_i)\cdot \min(\text{PortfolioMW}, Q^{proc}_t)\cdot \Delta h$.
- Cap: $R^{cap,capped}_t = \min(R^{cap}_t, B_t)$.
- Bound flag: $1$ if $R^{cap}_t > B_t$.

**Sanity properties (write tests):**

- For **fixed market/predictors**, $p_{\text{acc}}(K)$ **decreases** as $K$ increases.
- For **fixed K**, $p_{\text{acc}}$ **increases** when the modeled price level $\mu_t$ increases.
- **Bound share** should **not increase** when K decreases (typical case); it concentrates in high $P^{cap}$ intervals.

------

## 6) Outputs

- `*_intervals.csv`: `dt, Pcap, Qproc, p_acc(K_i), R_cap, B, R_cap_capped, bound_flag, …`
- `*_daily.csv`, `*_monthly.csv`: aggregates of expected accepted MWh and PLN (baseline & capped).
- `*_summary.json`: all headline metrics; for activation leg include `L_pln_per_MWh`, `theta_sweep` with `premium_min` and `premium_rec`.
- Charts payload (compact arrays) for front-ends.

------

## 7) Implementation Notes

- **Alignment**: forward-fill predictors; do **not** invent capacity price or procured MW where missing — drop or mark NA intervals.
- **Ridge**: standardize predictors internally if scales differ wildly; return de-standardized $\beta$ for readability.
- **Empirical mode**: allow a rolling window (e.g., 30/90 days) for $p_{\text{acc}}$ estimation as a toggle.
- **Banding**: validate $\sum_i v_i = 1$; enforce strictly increasing $K_i$.
- **KMB**: optional load and report **total** for context; **do not** use as a hard cap (it spans FCR→RR). Budget cap remains interval-level via $B_t$.

------

## 8) Minimal Tests (must pass)

1. **Unit test — 15-min vs hourly**: a synthetic constant price at 100 PLN/MW-h yields identical totals whether input is per-interval or per-hour once normalized.
2. **Monotonicity of $p_{\text{acc}}(K)$**: for fixed $\mu,\sigma$, verify $p_{\text{acc}}(100) > p_{\text{acc}}(200) > p_{\text{acc}}(300)$.
3. **Budget binding location**: with a synthetic spike day (high $P^{cap}$), verify bound flags occur on spike intervals; lowering $K$ does **not** increase bound share.
4. **Banding arithmetic**: two bands $(v=0.5,K=200)$, $(0.5,300)$ produce revenue between the single-K extremes in expectation.
5. **Activation L**: with $S^{bal}\equiv 100$, $K^{alt}\equiv 120$, computed $L=20$ and `premium_min(θ)=20θ` exactly.

------

## 9) Nice-to-haves (if time allows)

- Add a tiny JSON block in the summary that shows **K-ladder** $[K \mapsto p_{\text{acc}}(K), K\cdot p_{\text{acc}}(K)]$ for quick bid tuning.
- Include a **CF multiplier** and an **uplift factor** $(\text{CF}_{\text{high-price}}/\text{CF})$ as optional post-scalars; do not mix them into model internals.

------

**Deliverables:** a single Python module with the `value_afrr_down(...)` API, a CLI wrapper, CSVs/JSON outputs, and a short `README` showing one end-to-end run.

**Key principle:**
 Capacity leg = **pay-as-bid option premium** (what you invoice).
 Energy leg = **short put** on balancing energy spread (activation risk sensitivity).
 **Budget** caps expected **pay-as-bid** revenue per interval using the **actual aFRR-down price and procured MW**.