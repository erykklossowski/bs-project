#!/usr/bin/env bash
# Example command to run Black-Scholes model with PSE data

python ../black-scholes_refactored.py \
    --prices afrr_prices.csv --prices-dt-col dt --prices-col price_pln_per_mw_h --prices-unit per_mw_h \
    --vols afrr_volumes.csv --vols-dt-col dt --vols-mw-col mw_procured \
    --predictors predictors.csv --pred-dt-col dt \
    --pred-cols CEN,COR,CSDAC,CEB_SR,CKOEB \
    --K 400 --portfolio-mw 50 --ridge 1.0 --mode both --out-prefix pse_afrr_K400
