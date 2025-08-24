#!/usr/bin/env bash
# Example command to run Black-Scholes model with comprehensive PSE energy data
# Full year analysis: July 2024 - June 2025

python3 ../black-scholes_refactored.py \
    --prices afrr_prices.csv --prices-dt-col dt --prices-col price_pln_per_mw_h --prices-unit per_mw_h \
    --vols afrr_volumes.csv --vols-dt-col dt --vols-mw-col mw_procured \
    --predictors predictors.csv --pred-dt-col dt \
    --pred-cols CEN,COR,CSDAC,CEB_SR,SK \
    --K 400 --portfolio-mw 50 --ridge 1.0 --mode both --out-prefix energy_full_year_K400
