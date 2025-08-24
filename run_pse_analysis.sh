#!/usr/bin/env bash
# Complete PSE data pipeline for Black-Scholes analysis

set -euo pipefail

echo "=== PSE Data Pipeline for Black-Scholes Analysis ==="

# Step 1: Fetch raw data from PSE API
echo "Step 1: Fetching raw data from PSE..."
./fetch_pse_data.sh

# Step 2: Process raw data into structured format
echo "Step 2: Processing raw data..."
python process_pse_data.py

# Step 3: Run Black-Scholes analysis
echo "Step 3: Running Black-Scholes analysis..."
cd pse_processed
./run_example.sh
cd ..

echo "=== Pipeline completed successfully! ==="
echo "Results available in pse_processed/ directory"