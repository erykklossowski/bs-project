# aFRR-DOWN Valuation with Black-Scholes Option Pricing

This project provides a comprehensive framework for valuing aFRR-DOWN (automatic Frequency Restoration Reserve - Down) capacity using Black-Scholes-style option pricing methodology with integrated real-time PSE market data.

## Overview

The system implements:
- **Ex-ante valuation**: Option pricing using lognormal price models with predictive modeling
- **Ex-post valuation**: Realized payoff calculations for performance assessment
- **Budget constraints**: Portfolio-level budget capping based on market capacity
- **Real-time data**: Direct integration with PSE API for live market data
- **Time alignment**: Robust handling of 15-minute market intervals
- **Multiple input formats**: Support for CSV, Excel, and JSON files

## Complete Workflow

### Method 1: Direct aFRR Data Pipeline (Recommended)

**Step 1: Fetch aFRR-Down Data from PSE**
```bash
# Fetch actual aFRR-down prices and volumes for full year (July 2024 - June 2025)
./fetch_pse_data.sh
```
This downloads:
- **aFRR-down capacity prices** (afrr_d): Primary underlying for Black-Scholes
- **aFRR-down procurement volumes** (zmb_afrrd): Actual MW for budget constraints
- **Market predictors** (CEN, COR, SK, CSDAC): For μₜ drift modeling only

**Step 2: Process Data for Black-Scholes Analysis**
```bash
# Convert raw aFRR data to structured format
python3 process_pse_data.py
```

**Step 3: Run Black-Scholes Analysis**
```bash
cd pse_processed
python3 ../black-scholes_refactored.py \
    --prices afrr_prices.csv --prices-dt-col dt --prices-col price_pln_per_mw_h --prices-unit per_mw_h \
    --vols afrr_volumes.csv --vols-dt-col dt --vols-mw-col mw_procured \
    --predictors predictors.csv --pred-dt-col dt --pred-cols CEN,COR,SK,CSDAC \
    --K 400 --portfolio-mw 50 --ridge 1.0 --mode both --out-prefix corrected_afrr_K400
```

### Method 2: Legacy Energy Prices Endpoint (For Reference)

```bash
# Alternative: Use comprehensive energy-prices endpoint (CEN as proxy)
node energy-prices-downloader.js
python3 process_energy_prices.py
cd pse_processed_full && ./run_example.sh
```

## PSE Data Integration Features

### PSE Dataset Structure and Usage (CORRECTED ARCHITECTURE)

The system fetches actual aFRR-down market data from PSE's API endpoints, using real capacity prices and procurement volumes. Here's exactly what data is retrieved and how it's used:

#### Primary Data Sources

**1. aFRR-Down Capacity Prices** (cmbu-tu endpoint):
```json
{
  "dtime_utc": "2024-06-30 22:00:00",    // UTC timestamp
  "business_date": "2024-06-30",         // Trading day
  "period": "23:45 - 24:00",             // 15-minute period
  "afrr_d": 488.0,                       // aFRR-down capacity price [PLN/MW-h] ★ PRIMARY
  "afrr_g": 499.0,                       // aFRR-up capacity price [PLN/MW-h]
  "fcr_d": 361.0,                        // FCR-down price [PLN/MW-h]
  "fcr_g": 399.0                         // FCR-up price [PLN/MW-h]
}
```

**2. aFRR-Down Procurement Volumes** (zmb endpoint):
```json
{
  "dtime_utc": "2024-06-30 22:00:00",    // UTC timestamp
  "zmb_afrrd": 550.0,                    // aFRR-down procured MW ★ PRIMARY
  "zmb_afrrg": 550.0,                    // aFRR-up procured MW
  "zmb_fcrd": 200.0,                     // FCR-down procured MW
  "zmb_fcrg": 200.0                      // FCR-up procured MW
}
```

**3. Market Predictors** (price-cost endpoint):
```json
{
  "dtime_utc": "2024-06-30 22:00:00",    // UTC timestamp
  "cen_cost": 605.54,                    // Central Energy Market predictor
  "cor_pp_cost": 10.0,                   // Corrective pricing predictor
  "sk_cost": -197.474,                   // System cost predictor
  "ceb_sr_cost": 605.54                  // Balancing energy predictor
}
```

#### Data Processing and Usage

**1. PRIMARY UNDERLYING PRICE: afrr_d → S(t)**
- **Source Field**: `afrr_d` (actual aFRR-down capacity marginal price)
- **Processing Function**: `process_afrr_prices()` in `process_pse_data.py:65-92`
- **Usage**: 
  ```python
  # Lines 80-83: Use actual aFRR-down price as underlying
  result = df[['dt', 'afrr_d']].copy()
  result = result.rename(columns={'afrr_d': 'price_pln_per_mw_h'})
  ```
- **Analysis Role**: Used as S(t) in Black-Scholes formula E[(S-K)^+] via `bs_call_lognormal()` in `black-scholes_refactored.py:160-184`

**2. ACTUAL PROCUREMENT VOLUMES: zmb_afrrd → Budget Constraints**
- **Source Field**: `zmb_afrrd` (real aFRR-down MW procured by PSE)
- **Processing Function**: `process_afrr_volumes()` in `process_pse_data.py:95-122`
- **Usage**:
  ```python
  # Lines 110-113: Use actual procurement for budget
  result = df[['dt', 'zmb_afrrd']].copy()
  result = result.rename(columns={'zmb_afrrd': 'mw_procured'})
  ```
- **Budget Formula**: `price_afrrd × mw_procured × 0.25h = interval_budget_PLN`
- **Analysis Role**: Real budget constraints via explicit formula in `black-scholes_refactored.py:331-345`

**3. MARKET PREDICTORS: CEN/COR/SK → μₜ Modeling**
- **Source Fields**: `cen_cost`, `cor_pp_cost`, `sk_cost`, `ceb_sr_cost`
- **Processing Function**: `process_predictors()` in `process_pse_data.py:125-185`
- **Usage**:
  ```python
  # Lines 146-160: Map predictors for drift modeling
  col_mapping = {
      'cen_cost': 'CEN',       # Predictor only
      'cor_pp_cost': 'COR',    # Predictor only  
      'sk_cost': 'SK',         # Predictor only
      'ceb_sr_cost': 'CEB_SR'  # Predictor only
  }
  ```
- **Analysis Role**: Fed into `design_matrix()` → `fit_ridge()` → μₜ estimation (NOT underlying price)

## Units & Cadence Policy

**Standardization**: All prices are normalized to **PLN/MW-h** regardless of input format. The system handles different input units automatically:
- **Per-interval data**: Divided by interval duration (Δh = 0.25h for 15-minute intervals)
- **Hourly data**: Expanded across four 15-minute quarters using forward-fill
- **Mixed granularity**: Aligned to 15-minute intervals via `align_to_quarter()` function

**Time Alignment**: All datasets are synchronized to PSE's 15-minute market intervals (UTC timestamps), ensuring consistent temporal analysis across price, volume, and predictor data. Forward-fill interpolation maintains data continuity during market gaps.

#### Analysis Integration

**Black-Scholes Model Integration** (`black-scholes_refactored.py`):

1. **Price Normalization** (Lines 99-116):
   ```python
   # normalize_price_units() ensures all prices are in PLN/MW-h
   # Handles afrr_d price conversion and unit standardization
   ```

2. **Time Alignment** (Lines 78-96):
   ```python
   # align_to_quarter() synchronizes all PSE data to 15-minute intervals
   # Uses forward-fill (ffill) for market data continuity
   ```

3. **Predictor Modeling** (Lines 184-205):
   ```python
   # design_matrix() creates regression matrix from CEN,COR,CSDAC,CEB_SR,SK
   # fit_ridge() estimates μₜ coefficients for each predictor variable
   ```

4. **Option Valuation** (Lines 160-184):
   ```python
   # bs_call_lognormal() computes E[(S-K)^+] using afrr_d as underlying price S(t)
   # Uses PSE-derived volatility (σ) and time-varying drift (μₜ) from predictors
   ```

**Budget Constraint Integration**:
- **Volume Data**: Real zmb_afrrd MW procurement from PSE market data
- **Price Data**: afrr_d used for interval-by-interval budget calculations
- **Explicit Formula**: `price_afrrd × mw_procured_afrrd × 0.25h = interval_budget_PLN`
- **Function**: `cap_column()` applies market capacity constraints using actual PSE volumes

#### Data Architecture: Direct aFRR-Down Integration (CORRECTED)

**Primary Data Sources** (No Proxies):

1. **Underlying Price**: **afrr_d** field from PSE `cmbu-tu` endpoint provides actual aFRR-down capacity marginal prices. This is the real market price used as S(t) in Black-Scholes formula E[(S-K)^+], not a proxy.

2. **Procurement Volumes**: **zmb_afrrd** field from PSE `zmb` endpoint provides actual MW procured for aFRR-down reserves. This is real market data used for budget constraints: `price × MW × hours`.

3. **Market Predictors**: **CEN/COR/SK** fields are used ONLY as predictor variables for μₜ drift modeling, NOT as underlying price proxies.

**Budget Formula**: Explicit calculation using real market data:
```
budget_interval = afrr_d_price × zmb_afrrd_mw × 0.25h
portfolio_cap = fair_share × budget_interval
```

**Validation**: This architecture uses actual aFRR-down market data throughout, eliminating proxy dependencies and ensuring economic accuracy of the Black-Scholes valuation.

### Data Processing Pipeline
1. **JavaScript Fetcher**: `energy-prices-downloader.js`
   - Handles API pagination automatically
   - Implements retry logic and rate limiting
   - Downloads complete historical dataset
   
2. **Python Processor**: `process_energy_prices.py`
   - Converts comprehensive energy data to aFRR-specific format
   - Creates synthetic volume data based on market patterns
   - Processes predictor variables for modeling
   
3. **Black-Scholes Engine**: `black-scholes_refactored.py`
   - Performs ex-ante and ex-post valuation
   - Handles budget constraints and portfolio sizing
   - Generates comprehensive analytics and visualizations

## Installation

### Prerequisites
1. **Python 3.7+**: For Black-Scholes analysis engine
2. **Node.js**: For PSE data fetching (energy-prices-downloader.js)
3. **System Dependencies**:
   ```bash
   # Optional: for legacy bash scripts only
   curl jq
   ```

### Python Dependencies
```bash
pip install pandas numpy scipy matplotlib openpyxl
# Or use requirements file:
pip install -r requirements.txt
```

## Black-Scholes Model Parameters

### Core Pricing Parameters
- **`--K`** *(float)*: **Strike price** in PLN/MW-h 
  - Default: `400.0`
  - Typical range: `100-1000` PLN/MW-h
  - Example: `--K 450` for 450 PLN/MW-h strike

- **`--portfolio-mw`** *(float)*: **Portfolio size** in MW
  - Default: `1.0` MW
  - Typical range: `10-100` MW
  - Example: `--portfolio-mw 50` for 50 MW portfolio

### Analysis Modes
- **`--mode`** *(string)*: **Valuation mode**
  - Options: `exante`, `expost`, `both`
  - Default: `exante`
  - `exante`: Predictive Black-Scholes option pricing
  - `expost`: Realized payoff analysis
  - `both`: Complete analysis with both methods

### Model Configuration
- **`--ridge`** *(float)*: **Ridge regularization** parameter for predictor model
  - Default: `1.0`
  - Range: `0.0-10.0` (0 = no regularization)
  - Higher values = more conservative predictions

### Data Input Parameters

**Price Data (Required)**
- **`--prices`** *(string)*: Path to aFRR-down price data file
  - Supports: CSV, Excel (.xlsx), JSON
  - Example: `--prices afrr_prices.csv`

- **`--prices-dt-col`** *(string)*: Datetime column name
  - Example: `--prices-dt-col dt`

- **`--prices-col`** *(string)*: Price column name  
  - Example: `--prices-col price_pln_per_mw_h`

- **`--prices-unit`** *(string)*: Price unit specification
  - Options: `auto`, `per_mw_h`, `per_mw_interval`
  - Default: `auto` (assumes PLN/MW-h)

**Volume Data (Required)**
- **`--vols`** *(string)*: Path to procurement volume data
  - Example: `--vols afrr_volumes.csv`

- **`--vols-dt-col`** *(string)*: Datetime column name
  - Example: `--vols-dt-col dt`

- **`--vols-mw-col`** *(string)*: MW procurement column name
  - Example: `--vols-mw-col mw_procured`

**Predictor Data (Optional)**
- **`--predictors`** *(string)*: Path to predictor variables file
  - Example: `--predictors predictors.csv`

- **`--pred-dt-col`** *(string)*: Datetime column name
  - Example: `--pred-dt-col dt`

- **`--pred-cols`** *(string)*: Comma-separated predictor column names
  - Available: `CEN,COR,CSDAC,CEB_SR,SK`
  - Example: `--pred-cols CEN,COR,CSDAC,SK`

### Output Configuration
- **`--out-prefix`** *(string)*: Output file prefix
  - Default: `affr_down`
  - Example: `--out-prefix energy_K400_analysis`

## Example Usage Scenarios

### Scenario 1: Basic aFRR-down Valuation (K=400 PLN)
```bash
python3 black-scholes_refactored.py \
    --prices data/afrr_prices.csv --prices-dt-col timestamp --prices-col price \
    --vols data/afrr_volumes.csv --vols-dt-col timestamp --vols-mw-col mw_procured \
    --K 400 --portfolio-mw 25 --mode expost --out-prefix basic_K400
```

### Scenario 2: Full Predictive Analysis with All Predictors
```bash
python3 black-scholes_refactored.py \
    --prices afrr_prices.csv --prices-dt-col dt --prices-col price_pln_per_mw_h \
    --vols afrr_volumes.csv --vols-dt-col dt --vols-mw-col mw_procured \
    --predictors predictors.csv --pred-dt-col dt --pred-cols CEN,COR,CSDAC,CEB_SR,SK \
    --K 500 --portfolio-mw 75 --ridge 0.5 --mode both --out-prefix advanced_K500
```

### Scenario 3: Conservative High-Strike Analysis
```bash
python3 black-scholes_refactored.py \
    --prices afrr_prices.csv --prices-dt-col dt --prices-col price_pln_per_mw_h \
    --vols afrr_volumes.csv --vols-dt-col dt --vols-mw-col mw_procured \
    --predictors predictors.csv --pred-dt-col dt --pred-cols CEN,CSDAC \
    --K 800 --portfolio-mw 100 --ridge 2.0 --mode both --out-prefix conservative_K800
```

## Input File Formats

### Supported Formats
- **CSV**: Standard comma-separated values
- **Excel**: .xlsx and .xls files
- **JSON**: Array of objects or object with array values

### Required Columns
- **Price file**: Must contain datetime and price columns
- **Volume file**: Must contain datetime and MW columns
- **Predictor file**: Must contain datetime and feature columns

## Output Files and Analysis Results

The system generates comprehensive analysis outputs:

### Generated Files
1. **`{prefix}_intervals.csv`**: Detailed 15-minute interval data with pricing, volumes, and valuations
2. **`{prefix}_daily.csv`**: Daily aggregated performance summaries
3. **`{prefix}_monthly.csv`**: Monthly performance analysis for seasonal patterns
4. **`{prefix}_summary.json`**: Complete JSON summary with all key metrics
5. **`{prefix}_price.png`**: Price time series visualization
6. **`{prefix}_mw_procured.png`**: Volume procurement visualization  
7. **`{prefix}_p_accept_exante.png`**: Ex-ante acceptance probability plot (predictive mode)

### Key Performance Metrics

**Ex-ante Valuation (Predictive Black-Scholes)**
- **`exante_sigma`**: Market volatility parameter (higher = more volatile)
- **`exante_beta`**: Ridge regression coefficients for predictors
- **`exante_expected_MWh`**: Expected capacity accepted over analysis period
- **`exante_total_PLN_perMW`**: Total expected value per MW
- **`exante_portfolio_*_PLN`**: Portfolio-level valuations (baseline vs budget-capped)

**Ex-post Valuation (Realized Performance)**  
- **`expost_expected_MWh`**: Actual capacity accepted (price ≥ K)
- **`expost_total_PLN_perMW`**: Realized value per MW
- **`expost_portfolio_*_PLN`**: Actual portfolio performance

**Risk and Budget Analysis**
- **`share_bound_intervals_%`**: Percentage of time budget constraints were active
- **`avg_scale_factor`**: Average impact of budget limitations (1.0 = no impact)
- **Price range analysis**: Min/max/median pricing over analysis period

### Example Results Interpretation

For K=400 PLN analysis over July 2024-June 2025:
```json
{
  "K": 400.0,
  "exante_sigma": 0.91,           // High volatility market
  "expost_expected_MWh": 5586.0,  // 5,586 MWh accepted at strike
  "expost_total_PLN_perMW": 1142479,  // 1.14M PLN value per MW
  "exante_portfolio_baseline_total_PLN": 244630676,  // 244M PLN uncapped
  "exante_portfolio_capped_total_PLN": 135831853,    // 135M PLN after budget
  "share_bound_intervals_%": 19.16  // Budget binding 19% of time
}
```

## Technical Details

### Time Alignment
The script automatically aligns all input data to 15-minute intervals using forward-fill interpolation. This ensures consistent time series analysis regardless of input granularity.

### Price Normalization
Prices are normalized to PLN/MW-h units. The script handles different input units:
- `per_mw_h`: Already in correct units
- `per_mw_interval`: Per-interval pricing that gets converted
- `auto`: Assumes per_mw_h (most common case)

### Black-Scholes Implementation
The option pricing uses a lognormal price model:
- **μ_t**: Time-varying drift estimated from predictors
- **σ**: Global volatility parameter estimated from residuals
- **K**: Strike price (user-defined)

### Budget Constraints
Portfolio values are capped based on market capacity:
```
cap_share = budget_interval × min(1, portfolio_MW / market_MW)
```

## Error Handling

The script includes comprehensive error handling for:
- Missing or invalid input files
- Column name mismatches
- Data type conversion issues
- Mathematical computation errors

## Performance Considerations

- **Memory usage**: Scales linearly with data size
- **Computation time**: Dominated by matrix operations for large datasets
- **File I/O**: Optimized for sequential processing

## Project Structure

```
bs-project/
├── energy-prices-downloader.js     # PSE API data fetcher (Node.js)
├── process_energy_prices.py        # Energy data processor 
├── black-scholes_refactored.py     # Core Black-Scholes analysis engine
├── fetch_pse_data.sh               # Legacy individual endpoint fetcher
├── process_pse_data.py             # Legacy data processor
├── run_pse_analysis.sh             # Legacy complete pipeline
├── pse_data_js/                    # JavaScript fetcher output
│   └── energy_prices.json          # Complete PSE dataset (35K+ records)
├── pse_processed_full/             # Processed data for analysis  
│   ├── afrr_prices.csv            # aFRR-down capacity prices
│   ├── afrr_volumes.csv           # Procurement volumes (synthetic)
│   ├── predictors.csv             # Market predictor variables
│   └── energy_full_year_K400_*    # Analysis outputs
└── requirements.txt                # Python dependencies
```

## Performance and Scale

**Data Volume**: 35,000+ market intervals (July 2024 - June 2025)
**Processing Time**: ~30 seconds for complete year analysis
**Memory Usage**: ~100MB for full dataset processing
**Output Size**: ~50MB total analysis files

## Dependencies

### Core Analysis Engine
- **pandas**: Data manipulation and time series alignment
- **numpy**: Numerical computing and linear algebra
- **scipy**: Statistical functions (normal distribution, optimization)
- **matplotlib**: Time series visualization and plotting
- **openpyxl**: Excel file support for data inputs

### Data Fetching
- **Node.js**: For energy-prices-downloader.js
- **curl/jq**: For legacy bash scripts (optional)

## License

This project is provided as-is for educational and research purposes in energy market analysis.

## Contributing

Contributions welcome! Please ensure:
- Code follows established patterns (PEP 8 for Python, ESLint for JavaScript)
- New features include appropriate documentation
- Analysis results are validated against known market behavior
- PSE API integration respects rate limits and terms of use
