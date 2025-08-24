#!/usr/bin/env bash
# Black-Scholes aFRR-down valuation with CORRECTED architecture
#
# UNDERLYING PRICE: afrr_d (actual aFRR-down capacity marginal price)
# PROCUREMENT VOLUMES: zmb_afrrd (actual procured MW for budget constraints)  
# PREDICTORS: CEN,COR,SK,CSDAC (for μₜ drift modeling ONLY)
#
# Budget formula: price_afrrd × mw_procured_afrrd × Δh per interval
# Portfolio cap: revenues ≤ fair_share × interval_budget

python3 ../black-scholes_refactored.py \
    --prices afrr_prices.csv --prices-dt-col dt --prices-col price_pln_per_mw_h --prices-unit per_mw_h \
    --vols afrr_volumes.csv --vols-dt-col dt --vols-mw-col mw_procured \
    --predictors predictors.csv --pred-dt-col dt \
    --pred-cols CEN,COR,CEB_SR,CSDAC \
    --K 400 --portfolio-mw 50 --ridge 1.0 --mode both --out-prefix corrected_afrr_K400

# Key corrections from previous version:
# - Underlying price: afrr_d (not CEN cost proxy)
# - Volumes: zmb_afrrd actual data (not synthetic)  
# - Predictors: CEN/COR/SK for μₜ only (not underlying)
# - Budget: explicit price × MW × hours formula
