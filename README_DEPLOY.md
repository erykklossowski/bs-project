# aFRR-down Black-Scholes Web Application

A web-based interface for aFRR-down capacity valuation using Black-Scholes option pricing methodology with real PSE market data integration.

## 🚀 Quick Start

### Local Development

1. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Run the application:**
   ```bash
   python app.py
   ```

3. **Access the web interface:**
   Open your browser to `http://localhost:8000`

### Using the Application

1. **Upload Data Files:**
   - **Price Data**: aFRR-down capacity prices (afrr_d field from PSE cmbu-tu endpoint)
   - **Volume Data**: Actual procurement volumes (zmb_afrrd field from PSE zmb endpoint)
   - **Predictors** (Optional): Market variables (CEN, COR, SK, CSDAC) for μₜ modeling

2. **Configure Parameters:**
   - **Strike Price K**: Option strike in PLN/MW-h (default: 400)
   - **Portfolio Size**: MW capacity (default: 50)
   - **Ridge λ**: Regularization parameter (default: 1.0)
   - **Analysis Mode**: exante, expost, or both

3. **Run Analysis:**
   - Click "Run Black-Scholes Analysis"
   - View interactive charts and metrics
   - Download results (CSV, JSON formats)

## 📊 Features

### Complete Command-Line Functionality in Web UI

All parameters from `black-scholes_refactored.py` are available:

- **Data Input**: Support for CSV, Excel, JSON formats
- **Time Alignment**: Automatic 15-minute interval synchronization
- **Price Normalization**: PLN/MW-h unit standardization
- **Budget Constraints**: Explicit price × MW × hours formula
- **Dual Valuation**: Ex-ante (predictive) and ex-post (realized) analysis
- **Interactive Visualization**: Real-time charts with Plotly
- **Output Export**: Downloadable results in multiple formats

### Data Architecture (Corrected)

- **Underlying Price**: afrr_d (actual aFRR-down capacity marginal price) → S(t)
- **Volumes**: zmb_afrrd (real MW procured) → budget constraints
- **Predictors**: CEN/COR/SK (market variables) → μₜ drift modeling only
- **Budget Formula**: interval_budget = price × MW × 0.25h

## 🏗️ Architecture

```
├── app.py                   # FastAPI web application
├── valuation_core.py        # Black-Scholes core functions
├── templates/
│   ├── index.html          # Main web interface
│   └── result.html         # Results display
├── static/                 # CSS/JS assets (auto-created)
├── outputs/                # Generated analysis files
├── requirements.txt        # Python dependencies
└── render.yml             # Render.com deployment
```

## 🔧 API Endpoints

- **GET /**: Main web interface
- **POST /run**: Execute Black-Scholes analysis
- **GET /health**: Health check
- **Static files**: `/outputs/` for downloads

## 🌐 Deployment

### Render.com (Recommended)

1. Connect your repository to Render.com
2. Use the included `render.yml` blueprint
3. Deploy automatically with zero configuration

### Manual Deployment

```bash
# Production server
uvicorn app:app --host 0.0.0.0 --port 8000

# With Gunicorn
gunicorn -w 4 -k uvicorn.workers.UvicornWorker app:app
```

## 📈 Analysis Examples

### Basic Analysis (K=400 PLN)
- Upload aFRR price and volume files
- Set strike to 400 PLN/MW-h
- Choose "both" mode for complete analysis
- Download interval and summary data

### Advanced Predictive Modeling
- Include market predictor file (CEN, COR, SK, CSDAC)
- Adjust ridge regularization (λ = 0.5-2.0)
- Use "exante" mode for forward-looking valuation
- Monitor budget-bound intervals percentage

## 🛠️ Technical Details

- **Framework**: FastAPI + Jinja2 templates
- **Data Processing**: pandas + numpy + scipy
- **Visualization**: Plotly.js interactive charts
- **File Support**: CSV, Excel (.xlsx/.xls), JSON
- **Time Handling**: UTC timezone alignment to 15-minute intervals
- **Mathematical Core**: Lognormal Black-Scholes with time-varying drift

## ✅ Validation

The web application provides identical results to the command-line version:

```bash
# Command line equivalent
python black-scholes_refactored.py \
    --prices afrr_prices.csv --prices-dt-col dt --prices-col afrr_d \
    --vols afrr_volumes.csv --vols-dt-col dt --vols-mw-col zmb_afrrd \
    --predictors predictors.csv --pred-dt-col dt --pred-cols CEN,COR,SK \
    --K 400 --portfolio-mw 50 --mode both
```

All parameters are accessible through the web interface with real-time validation and error handling.

## 🔍 Troubleshooting

- **File Upload Issues**: Check file format (CSV/Excel/JSON) and column names
- **Time Alignment Errors**: Ensure datetime columns are properly formatted
- **Analysis Failures**: Verify data overlap between price and volume files
- **Performance**: Large datasets (>100k rows) may take 30+ seconds to process

## 📄 License

Educational and research use for energy market analysis.