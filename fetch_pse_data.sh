#!/usr/bin/env bash
set -euo pipefail

BASE="https://api.raporty.pse.pl/api"
START="${START:-2024-07-01}"   # inclusive trading day
END="${END:-2025-06-30}"      # inclusive trading day
TOP="${TOP:-1000}"            # page size
OUTDIR="${OUTDIR:-pse_raw}"
mkdir -p "$OUTDIR"

# Paginated fetcher for PSE API. Gets all available data following nextLink.
fetch_paginated () {
  local ep="$1" fname="$2"
  local page=0 total=0
  local url="${BASE}/${ep}?\$first=${TOP}&\$filter=dtime%20ge%20'${START}'%20and%20dtime%20le%20'${END}'"
  
  while [[ -n "$url" ]]; do
    local out="${OUTDIR}/${fname}_p${page}.json"
    echo "[GET page ${page}] $url"
    curl -sS "$url" -o "$out"

    # Count items in this page
    local count=$(jq 'if type=="array" then length elif has("value") then (.value|length) else 0 end' "$out")
    echo "  -> ${count} rows"
    
    if [[ $count -eq 0 ]]; then
      rm -f "$out"
      break
    fi
    
    total=$((total + count))
    
    # Get next URL from nextLink field
    url=$(jq -r '.nextLink // empty' "$out")
    page=$((page + 1))
    
    # Safety limit to prevent infinite loops
    if [[ $page -gt 1000 ]]; then
      echo "  -> Safety limit reached (1000 pages), stopping"
      break
    fi
  done
  
  echo "TOTAL ${fname}: ${total} rows across ${page} pages"
}

echo "Fetching aFRR-down data from ${START} to ${END}"

# 1) aFRR-down capacity marginal prices (PRIMARY: afrr_d is the underlying price)
echo "=== Fetching aFRR-down capacity prices (afrr_d) ==="
fetch_paginated "cmbu-tu" "afrr-prices"

# 2) aFRR-down procurement volumes (REQUIRED: zmb_afrrd is actual procured MW)
echo "=== Fetching aFRR-down procurement volumes (zmb_afrrd) ==="
fetch_paginated "zmb" "afrr-volumes"

# 3) Market predictors for μₜ modeling (CEN, COR, SK as predictors ONLY)
echo "=== Fetching market predictors for drift modeling ==="
fetch_paginated "price-cost" "market-predictors"

# 4) SDAC prices (additional predictor)
echo "=== Fetching SDAC prices (additional predictor) ==="
fetch_paginated "csdac-pln" "sdac-prices"

echo "Done. Raw aFRR data in: $OUTDIR"
echo ""
echo "Data usage:"
echo "- afrr_d from afrr-prices: Primary underlying price for Black-Scholes"
echo "- zmb_afrrd from afrr-volumes: Actual procured MW for budget constraints"
echo "- CEN/COR/SK from market-predictors: Predictors for μₜ drift modeling only"