# aFRR-Down Valuation with Two Bids (Capacity Premium & Energy Strike)

## 0) Objective

Build a Python module + CLI that backtests and explores **two bid parameters** for a BSP in the Polish balancing market:

- **Capacity bid (K_cap, PLN/MW-h):** the **option premium** you bid. If **K_cap ≤ market capacity clearing price P_cap,t**, your capacity is selected in that interval; you are **paid K_cap** per MW-h for the procured share (pay-as-bid). No “clearing minus K” surplus.
- **Energy bid (K_energy, PLN/MWh, may be negative):** your **balancing energy strike** for activation merits. Activation occurs only if capacity is selected **and** your energy bid is competitive in the energy merit order. Energy payoff uses a configurable pay rule (default: **difference**).

The tool must:

1. Compute **ex-ante expected capacity revenue** for any (K_cap) or ladder of (v_i, K_cap,i).
2. Apply a **budget envelope** per interval based on **actual P_cap,t and procured MW** (no market distortion).
3. Provide an **energy leg** sensitivity using **balancing energy prices** and **K_energy**, supporting negative bids.
4. Support **two-parameter exploration/optimization** over (K_cap, K_energy).
5. Handle **15-minute** granularity, unit normalization, and negative energy prices robustly.

------

## 1) Inputs & Units

- Timezone: **UTC**. Align all series to a **15-min** grid. Let Δh = **0.25 h**.
- **Capacity price**: P_cap,t (PLN/MW-h). If source is PLN/MW **per 15-min interval**, convert: price_MW-h = price_interval / 0.25.
- **Procured aFRR-down MW**: Q_proc,t (MW).
- **Predictors (optional)** for ex-ante modeling of the **capacity price level** (CEN, COR, CSDAC, SK, etc.).
- **Balancing energy price (down)**: S_bal,t (PLN/MWh, can be **negative**).
- **(Optional) KMB**: for **context** only. Do **not** hard-cap capacity with total KMB (spans FCR→RR). Use it as a secondary, portfolio-level plausibility check. Primary cap is interval envelope (below).

------

## 2) Capacity Leg (Option Premium; Pay-as-Bid)

### Acceptance at K_cap

- Parametric mode (default): model ln P_cap,t ~ N(μ_t, σ²), where μ_t is a ridge regression on predictors; σ from residuals.

- Ex-ante **selection probability** at time t:
  $$
    p_{\text{cap},t}(K_{\text{cap}})=\Pr(P_{\text{cap},t}\ge K_{\text{cap}})=1-\Phi\!\left(\frac{\ln K_{\text{cap}}-\mu_t}{\sigma}\right).
  $$

- Empirical mode (fallback): use rolling empirical CDF of P_cap,t.

### Expected pay-as-bid **capacity revenue**

For a **single** capacity bid K_cap and **portfolio MW** $Q^{port}$:
$$
R^{\text{cap}}_t = K_{\text{cap}}\cdot p_{\text{cap},t}(K_{\text{cap}})\cdot \min(Q^{port}, Q_{\text{proc},t})\cdot \Delta h.
$$
For **banded** capacity bids $(v_i, K_{\text{cap},i}),\ \sum v_i = 1,\ K_{\text{cap},1}<\dots:
$$
R^{\text{cap}}_t = \sum_i v_i\,K_{\text{cap},i}\,p_{\text{cap},t}(K_{\text{cap},i})\cdot \min(Q^{port}, Q_{\text{proc},t})\cdot \Delta h.
$$

### **Budget envelope** (no distortion)

Per interval:
$$
B_t = P_{\text{cap},t}\cdot \min(Q^{port}, Q_{\text{proc},t})\cdot \Delta h.
$$
Cap expected capacity revenue per interval:
$$
R^{\text{cap,capped}}_t = \min\!\big(R^{\text{cap}}_t,\, B_t\big),
\quad
\text{bound}_t = \mathbf{1}\{R^{\text{cap}}_t > B_t\}.
$$
**Property:** lowering K_cap usually **reduces** $K\cdot p_{\text{cap}}$ and thus **reduces** bound share; binds concentrate in **high P_cap** intervals.

------

## 3) Energy Leg (Activation; Separate Bid & Merit)

### Energy acceptance at K_energy

- Activation only considered when **capacity is selected** (use p_cap,t as the capacity gate).

- Without full merit order depth, approximate **energy merit** via the balancing energy clearing price S_bal,t:
  $$
  p_{\text{eng},t}(K_{\text{energy}}) \approx \Pr(S_{\text{bal},t}\ge K_{\text{energy}}),
  $$
  using **empirical CDF** (energy prices can be negative → avoid lognormal).
   *Interpretation:* for down-regulation, more **negative** K_energy is cheaper → higher acceptance.

- Optional **activation volume rate** scenario $\theta$ (activated MWh per MW-h when energy is accepted). If actual activation traces exist, allow ingest to replace θ.

### Energy payoff per activated MWh (configurable rule)

Provide a switch `energy_pay_rule ∈ {'difference','pay_as_bid','pay_as_clear'}`:

- **difference (default):** payoff/MWh = $S_{\text{bal},t} - K_{\text{energy}}$
   (matches “tied to the difference between your strike and balancing energy price”; supports negative bids).
- **pay_as_bid:** payoff/MWh = $K_{\text{energy}}$.
- **pay_as_clear:** payoff/MWh = $S_{\text{bal},t}$.

Expected **energy** revenue per interval (portfolio):
$$
R^{\text{eng}}_t
= \Big[\sum_i v_i\,p_{\text{cap},t}(K_{\text{cap},i})\Big]
  \cdot p_{\text{eng},t}(K_{\text{energy}})
  \cdot \theta
  \cdot \min(Q^{port}, Q_{\text{proc},t})
  \cdot \underbrace{\text{payoff\_per\_MWh}_t}_{\text{per rule}} \cdot \Delta h.
$$

> Keep **energy** separate from the capacity budget envelope. Optionally, impose an **energy spend** plausibility cap using published balancing energy volumes/costs if available (off by default).

------

## 4) Total Expected Revenue & Two-Bid Exploration

Total per interval:
$$
R^{\text{total}}_t = R^{\text{cap,capped}}_t + R^{\text{eng}}_t.
$$
Aggregate to daily/monthly/total. Expose functions to:

- Sweep **K_cap** (and optionally **bands**) for fixed **K_energy**.
- Sweep **K_energy** for fixed **K_cap** (and θ scenarios).
- Produce a **2D surface** $(K_{\text{cap}}, K_{\text{energy}}) \mapsto \text{Total PLN}$.

------

## 5) API & CLI

### Core function

```
def value_afrr_down_two_bids(
    # Capacity data
    df_cap: pd.DataFrame, dt_cap: str, col_cap: str, cap_unit: str,   # 'per_mw_h' | 'per_mw_interval' | 'auto'
    df_vol: pd.DataFrame, dt_vol: str, col_vol_mw: str,

    # Predictors (optional)
    df_pred: pd.DataFrame | None = None, dt_pred: str | None = None, pred_cols: list[str] | None = None,
    ridge_lambda: float = 1.0, cap_model: str = "parametric",        # 'parametric' | 'empirical'

    # Capacity bids
    portfolio_mw: float = 1.0,
    cap_bands: list[tuple[float, float]] | None = None,               # [(v_i, K_cap_i)]
    single_K_cap: float | None = None,

    # Energy inputs/bid
    df_energy: pd.DataFrame | None = None, dt_eng: str | None = None, col_energy: str | None = None,
    single_K_energy: float | None = None,
    energy_pay_rule: str = "difference",                              # 'difference'|'pay_as_bid'|'pay_as_clear'
    theta: float = 0.01,                                              # activated MWh per MW-h when accepted (scenario)

    # Options
    return_intervals: bool = True,
) -> dict:
    """Returns summary dict + optional interval table for charts."""
```

### CLI

```
python afrr_two_bids.py \
  --cap cap.csv --cap-dt dt --cap-col afrr_d --cap-unit per_mw_h \
  --vol vol.csv --vol-dt dt --vol-mw zmb_afrrd \
  --pred pred.csv --pred-dt dt --pred-cols CEN,COR,CSDAC,SK --ridge 1.0 --cap-model parametric \
  --portfolio-mw 500 \
  --Kcap 250 \
  --Keng -50 --energy energy.csv --energy-dt dt --energy-col price_pln_per_MWh --energy-pay-rule difference --theta 0.01 \
  --out run1
```

Support `--cap-bands v1:K1,v2:K2,...` as an alternative to `--Kcap`.

------

## 6) Outputs

- `*_summary.json`:
  - `K_cap`, `K_energy`, `portfolio_mw`, `sigma_cap`, `betas_cap` (if parametric),
  - `capacity_pln_perMW_baseline`, `capacity_pln_portfolio_baseline`,
  - `capacity_pln_portfolio_capped`, `capacity_bound_intervals_pct`,
  - `energy_rule`, `theta`, `energy_pln_portfolio` (and per-MW),
  - `total_pln_portfolio` (cap+energy),
  - diagnostics: share(P_cap≥K_cap), price quantiles, inferred Δh.
- `*_intervals.csv`: `dt, P_cap, Q_proc, p_cap(K_i), R_cap, B, R_cap_capped, bound_flag, S_bal, p_eng(K_energy), payoff_per_MWh, R_eng, R_total`.
- `*_daily.csv`, `*_monthly.csv`: aggregated PLN and accepted MW-h stats.
- Chart payloads (lists) for quick Plotly: time series of P_cap vs K_cap, p_cap, procured MW & envelope vs expected capacity (capped), S_bal vs K_energy, and total PLN.

------

## 7) Validation & Tests

1. **Units**: identical totals when input capacity price is per-interval vs per-hour after normalization.
2. **Monotonicity**: for fixed μ,σ, $p_{\text{cap}}(K)$ decreases in K; for energy (empirical CDF), $p_{\text{eng}}(K_{\text{energy}})$ increases as K_energy becomes **more negative**.
3. **Budget binds**: occur in **high P_cap** intervals; lowering K_cap does **not** increase bound share in typical data.
4. **Negative energy prices**: `energy_pay_rule='difference'` works with negative S_bal and K_energy.
5. **Two-bid surface**: increasing |K_energy| negatively (more competitive) increases energy acceptance but lowers per-MWh payoff under ‘difference’; K_cap trade-off follows $K\cdot p_{\text{cap}}$.

------

## 8) Notes & Design Choices

- Capacity leg = **pay-as-bid option premium** (invoice reality).
- Energy leg = **merit-driven activation** with a configurable payoff rule; default to **difference** to reflect “revenue tied to the difference between your strike and the balancing price,” allowing **negative** K_energy (you get paid to take surplus).
- **Primary realism cap** is **interval envelope** $B_t$ from actual P_cap,t and Q_proc,t. KMB may be printed as context but not used to hard-cap aFRR-down capacity totals (spans other products).
- Keep capacity and energy results **separate** in the summary; sum for total only after both are computed.