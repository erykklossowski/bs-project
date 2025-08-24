# app.py
import os, io, uuid, json, subprocess
from pathlib import Path
from typing import Optional
from datetime import datetime, timedelta
import pandas as pd
from fastapi import FastAPI, Request, Form, HTTPException, BackgroundTasks, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from afrr_valuation import value_afrr_down
from afrr_two_bids import value_afrr_down_two_bids

app = FastAPI(title="aFRR-down Valuation Suite", version="2.0.0")

# Create directories if they don't exist
os.makedirs("static", exist_ok=True)
os.makedirs("templates", exist_ok=True)
os.makedirs("outputs", exist_ok=True)

app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/outputs", StaticFiles(directory="outputs"), name="outputs")
templates = Jinja2Templates(directory="templates")

# Global status for data fetching
fetch_status = {"status": "idle", "progress": 0, "message": "Ready to fetch data"}

async def fetch_pse_data(start_date: str, end_date: str):
    """Fetch PSE data using the Node.js downloader and process it."""
    global fetch_status
    
    try:
        fetch_status = {"status": "fetching", "progress": 10, "message": "Starting PSE data download..."}
        
        # Create output directories
        os.makedirs('pse_data_js', exist_ok=True)
        os.makedirs('pse_raw_auto', exist_ok=True)
        os.makedirs('pse_processed_auto', exist_ok=True)
        
        # Set environment variables for the Node.js script
        env = os.environ.copy()
        env['START'] = start_date
        env['END'] = end_date
        env['OUTDIR'] = 'pse_raw_auto'
        
        # Run the JavaScript downloader
        fetch_status = {"status": "fetching", "progress": 30, "message": "Downloading energy prices from PSE API..."}
        result = subprocess.run([
            'node', 'energy-prices-downloader.js'
        ], cwd=Path.cwd(), capture_output=True, text=True, env=env, timeout=300)
        
        if result.returncode != 0:
            print(f"JavaScript downloader failed: {result.stderr}")
            # Try to create mock data for testing if JS fails
            mock_data_file = Path("pse_data_js/energy_prices.json")
            if not mock_data_file.exists():
                print("Creating mock data for testing...")
                mock_data = [
                    {"dtime_utc": "2024-07-01 00:00:00", "cen_cost": 400.5, "cor_pp_cost": 10.2, "sk_cost": -5.1},
                    {"dtime_utc": "2024-07-01 00:15:00", "cen_cost": 425.3, "cor_pp_cost": 12.1, "sk_cost": -3.2},
                    {"dtime_utc": "2024-07-01 00:30:00", "cen_cost": 380.7, "cor_pp_cost": 8.9, "sk_cost": -7.3}
                ]
                with open(mock_data_file, 'w') as f:
                    json.dump(mock_data, f, indent=2)
        
        # Run bash script to fetch additional aFRR data
        fetch_status = {"status": "fetching", "progress": 50, "message": "Downloading aFRR-specific data..."}
        result = subprocess.run([
            'bash', 'fetch_pse_data.sh'
        ], cwd=Path.cwd(), capture_output=True, text=True, env=env, timeout=300)
        
        if result.returncode != 0:
            print(f"Warning: Direct aFRR fetch failed: {result.stderr}")
        
        # Process the downloaded data
        fetch_status = {"status": "processing", "progress": 70, "message": "Processing downloaded data..."}
        
        # Process energy prices data  
        subprocess.run([
            'python3', 'process_energy_prices.py',
            '--input', 'pse_data_js/energy_prices.json',
            '--output-dir', 'pse_processed_auto'
        ], cwd=Path.cwd(), check=True)
        
        # Also process direct aFRR data if available
        try:
            subprocess.run([
                'python3', 'process_pse_data.py',
                '--raw-dir', 'pse_raw_auto', 
                '--output-dir', 'pse_processed_auto'
            ], cwd=Path.cwd(), check=False, capture_output=True, text=True)
        except Exception as e:
            print(f"Direct aFRR processing failed (expected): {e}")
        
        fetch_status = {"status": "completed", "progress": 100, "message": "Data download and processing completed!"}
        
        # Check which files were created
        output_dir = Path("pse_processed_auto")
        files_created = []
        
        for file_pattern in ["afrr_prices.csv", "afrr_volumes.csv", "predictors.csv"]:
            file_path = output_dir / file_pattern
            if file_path.exists():
                files_created.append(str(file_path))
        
        return {
            "success": True, 
            "files_created": files_created,
            "message": f"Successfully downloaded data from {start_date} to {end_date}"
        }
        
    except subprocess.TimeoutExpired:
        fetch_status = {"status": "error", "progress": 0, "message": "Data download timed out"}
        raise HTTPException(status_code=408, detail="Data download timed out")
    except Exception as e:
        fetch_status = {"status": "error", "progress": 0, "message": f"Error: {str(e)}"}
        raise HTTPException(status_code=500, detail=f"Data fetch failed: {str(e)}")

def load_auto_data() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Load automatically fetched PSE data."""
    base_path = Path("pse_processed_auto")
    
    # Load price data
    price_file = base_path / "afrr_prices.csv"
    if price_file.exists():
        prices_df = pd.read_csv(price_file)
    else:
        raise ValueError("No aFRR price data found. Please fetch data first.")
    
    # Load volume data  
    vol_file = base_path / "afrr_volumes.csv"
    if vol_file.exists():
        vols_df = pd.read_csv(vol_file)
    else:
        raise ValueError("No aFRR volume data found. Please fetch data first.")
    
    # Load predictor data (optional)
    pred_file = base_path / "predictors.csv"
    preds_df = None
    if pred_file.exists():
        preds_df = pd.read_csv(pred_file)
    
    return prices_df, vols_df, preds_df

def read_upload(file: UploadFile | None) -> pd.DataFrame | None:
    """Read uploaded file into pandas DataFrame."""
    if file is None:
        return None
    data = file.file.read()
    name = (file.filename or "").lower()
    
    try:
        if name.endswith(".csv") or name.endswith(".txt"):
            return pd.read_csv(io.BytesIO(data))
        elif name.endswith(".xlsx") or name.endswith(".xls"):
            return pd.read_excel(io.BytesIO(data), engine="openpyxl")
        elif name.endswith(".json"):
            return pd.read_json(io.BytesIO(data))
        else:
            # Default to CSV
            return pd.read_csv(io.BytesIO(data))
    except Exception as e:
        print(f"Error reading file {name}: {e}")
        return None

def save_outputs(results: dict, prefix: str = "valuation") -> dict:
    """Save analysis outputs to files and return download links."""
    output_files = {}
    
    try:
        # Save intervals CSV if available
        if "intervals_df" in results:
            intervals_file = f"outputs/{prefix}_intervals.csv"
            results["intervals_df"].to_csv(intervals_file, index=False)
            output_files["intervals"] = {"name": "Intervals Data (CSV)", "path": f"/outputs/{prefix}_intervals.csv"}
        
        # Save summary JSON (excluding DataFrames)
        summary_file = f"outputs/{prefix}_summary.json"
        summary_data = {k: v for k, v in results.items() if k not in ["intervals_df", "chart_data"]}
        
        # Clean data for JSON serialization
        def clean_for_json(obj):
            # Handle None and Undefined objects
            if obj is None:
                return None
            
            # Handle Jinja2 Undefined objects
            if hasattr(obj, '__class__') and 'Undefined' in str(type(obj)):
                return None
            
            # Handle numpy types
            if hasattr(obj, 'item'):  # numpy scalar
                return obj.item()
            
            # Handle pandas/numpy arrays
            if hasattr(obj, 'tolist'):
                return obj.tolist()
            
            # Handle dictionaries
            if isinstance(obj, dict):
                return {k: clean_for_json(v) for k, v in obj.items()}
            
            # Handle lists and tuples
            elif isinstance(obj, (list, tuple)):
                return [clean_for_json(item) for item in obj]
            
            # Handle objects with __dict__
            elif hasattr(obj, '__dict__'):
                return obj.__dict__
            
            # Handle basic types and fallback
            elif isinstance(obj, (int, float, str, bool)):
                return obj
            
            else:
                # Convert other types to string as fallback
                try:
                    return str(obj)
                except:
                    return None
        
        summary_data_clean = clean_for_json(summary_data)
        
        with open(summary_file, 'w') as f:
            json.dump(summary_data_clean, f, indent=2, default=str)
        output_files["summary"] = {"name": "Summary (JSON)", "path": f"/outputs/{prefix}_summary.json"}
        
        # Create daily aggregates if we have enough data
        if "intervals_df" in results:
            df = results["intervals_df"]
            if len(df) > 24:
                df_copy = df.copy()
                df_copy['date'] = pd.to_datetime(df_copy['dt']).dt.date
                
                agg_dict = {
                    'price_pln_per_mw_h': ['mean', 'min', 'max'],
                    'mw_procured': 'mean'
                }
                
                # Add capacity revenue columns if they exist
                if 'capacity_revenue_baseline' in df_copy.columns:
                    agg_dict['capacity_revenue_baseline'] = 'sum'
                if 'capacity_revenue_capped' in df_copy.columns:
                    agg_dict['capacity_revenue_capped'] = 'sum'
                if 'budget_envelope' in df_copy.columns:
                    agg_dict['budget_envelope'] = 'sum'
                if 'bound_flag' in df_copy.columns:
                    agg_dict['bound_flag'] = 'sum'
                
                daily_agg = df_copy.groupby('date').agg(agg_dict).round(2)
                daily_agg.columns = ['_'.join(col).strip() for col in daily_agg.columns]
                
                daily_file = f"outputs/{prefix}_daily.csv"
                daily_agg.to_csv(daily_file)
                output_files["daily"] = {"name": "Daily Aggregates (CSV)", "path": f"/outputs/{prefix}_daily.csv"}
    
    except Exception as e:
        print(f"Error saving outputs: {e}")
    
    return output_files

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    # Check if auto data exists
    auto_data_available = all([
        Path("pse_processed_auto/afrr_prices.csv").exists(),
        Path("pse_processed_auto/afrr_volumes.csv").exists()
    ])
    
    return templates.TemplateResponse("index.html", {
        "request": request, 
        "summary": None, 
        "chart": None, 
        "download_files": None,
        "auto_data_available": auto_data_available,
        "fetch_status": fetch_status
    })

@app.post("/fetch-data")
async def trigger_data_fetch(
    background_tasks: BackgroundTasks,
    start_date: str = Form(default="2024-07-01"),
    end_date: str = Form(default="2025-06-30")
):
    """Trigger PSE data fetching in background."""
    global fetch_status
    
    if fetch_status["status"] == "fetching":
        return JSONResponse({"error": "Data fetch already in progress"}, status_code=400)
    
    # Reset fetch status
    fetch_status = {"status": "fetching", "progress": 0, "message": "Starting data fetch..."}
    
    # Add background task
    background_tasks.add_task(fetch_pse_data, start_date, end_date)
    
    return JSONResponse({"message": "Data fetch started", "status": "initiated"})

@app.get("/fetch-status")
async def get_fetch_status():
    """Get current status of data fetching."""
    return JSONResponse(fetch_status)

@app.post("/run", response_class=HTMLResponse)
async def run_analysis(
    request: Request,
    # Bidding parameters
    portfolio_mw: float = Form(50.0, description="Portfolio size (MW)"),
    bidding_mode: str = Form("single", description="Bidding mode: single or banded"),
    single_K: float = Form(400.0, description="Single bid level (PLN/MW-h)"),
    bands_input: str = Form("", description="Banded bids: v1:K1,v2:K2,..."),
    
    # Model parameters
    model_mode: str = Form("parametric", description="Model: parametric or empirical"),
    ridge: float = Form(1.0, description="Ridge regularization parameter"),
    
    # Energy leg parameters (optional)
    enable_energy_leg: bool = Form(False, description="Enable energy leg analysis"),
    theta_values: str = Form("0.005,0.01,0.02,0.05", description="Theta values (comma-separated)"),
    risk_uplift: float = Form(0.15, description="Risk uplift factor"),
    
    # Output parameters
    out_prefix: str = Form("web_valuation", description="Output file prefix"),
    use_auto_data: bool = Form(True, description="Use automatically fetched PSE data")
):
    """Run complete aFRR-down two-leg valuation analysis."""
    
    try:
        if use_auto_data:
            # Load automatically fetched PSE data
            try:
                prices_df, vols_df, preds_df = load_auto_data()
                
                # Fixed column names for auto data
                prices_dt_col = "dt"
                prices_col = "price_pln_per_mw_h"
                prices_unit = "per_mw_h"
                vols_dt_col = "dt" 
                vols_mw_col = "mw_procured"
                pred_dt_col = "dt"
                pred_cols_list = []
                
                if preds_df is not None:
                    # Get available predictor columns
                    available_predictors = [col for col in ["CEN", "COR", "CSDAC", "SK", "CEB_SR"] 
                                          if col in preds_df.columns]
                    pred_cols_list = available_predictors
                
            except ValueError as e:
                auto_data_available = False
                return templates.TemplateResponse("index.html", {
                    "request": request,
                    "error": f"Auto data not available: {str(e)}. Please fetch data first.",
                    "summary": None, 
                    "chart": None,
                    "download_files": None,
                    "auto_data_available": auto_data_available,
                    "fetch_status": fetch_status
                })
        else:
            # This path is no longer used but kept for compatibility
            return templates.TemplateResponse("index.html", {
                "request": request,
                "error": "Manual file upload is not supported. Please use automatic PSE data fetching.",
                "summary": None,
                "chart": None, 
                "download_files": None,
                "auto_data_available": False,
                "fetch_status": fetch_status
            })
        
        # Parse bidding parameters
        bids = None
        single_K_param = None
        
        if bidding_mode == "single":
            single_K_param = single_K
        elif bidding_mode == "banded" and bands_input.strip():
            try:
                bids = []
                for band in bands_input.split(','):
                    band = band.strip()
                    if ':' in band:
                        fraction_str, bid_str = band.split(':', 1)
                        bids.append((float(fraction_str), float(bid_str)))
                if not bids:
                    single_K_param = single_K  # Fallback to single
            except ValueError:
                single_K_param = single_K  # Fallback to single on parse error
        else:
            single_K_param = single_K  # Fallback to single
        
        # Parse theta values for energy leg
        theta_list = None
        if enable_energy_leg and theta_values.strip():
            try:
                theta_list = [float(x.strip()) for x in theta_values.split(',')]
            except ValueError:
                theta_list = [0.005, 0.01, 0.02, 0.05]  # Default values
        
        # Run new aFRR valuation
        results = value_afrr_down(
            # Capacity price & volumes
            df_cap=prices_df, dt_col_cap=prices_dt_col, price_col_cap=prices_col, price_unit=prices_unit,
            df_vol=vols_df, dt_col_vol=vols_dt_col, mw_col_vol=vols_mw_col,
            
            # Predictors
            df_pred=preds_df, dt_col_pred=pred_dt_col, pred_cols=pred_cols_list,
            ridge_lambda=ridge, model_mode=model_mode,
            
            # Bids
            portfolio_mw=portfolio_mw, bids=bids, single_K=single_K_param,
            
            # Energy leg (optional - we'll implement this later)
            theta_list=theta_list if enable_energy_leg else None,
            risk_uplift=risk_uplift,
            
            return_intervals=True
        )
        
        # Extract data for template compatibility
        intervals_df = results.get('intervals_df')
        summary = results  # Use full results as summary
        chart_data = results.get('chart_data', {})
        
        # Save outputs and create download links
        download_files = save_outputs(results, out_prefix)
        
        # Check auto data availability for template
        auto_data_available = all([
            Path("pse_processed_auto/afrr_prices.csv").exists(),
            Path("pse_processed_auto/afrr_volumes.csv").exists()
        ])
        
        return templates.TemplateResponse("index.html", {
            "request": request,
            "summary": summary,
            "chart": chart_data,
            "download_files": download_files,
            "auto_data_available": auto_data_available,
            "fetch_status": fetch_status,
            "params": {
                "portfolio_mw": portfolio_mw,
                "bidding_mode": bidding_mode,
                "single_K": single_K_param or single_K,
                "bands_input": bands_input,
                "model_mode": model_mode,
                "ridge": ridge,
                "enable_energy_leg": enable_energy_leg,
                "theta_values": theta_values,
                "pred_cols": ",".join(pred_cols_list) if pred_cols_list else "",
                "out_prefix": out_prefix
            }
        })

    except Exception as e:
        error_msg = f"Analysis failed: {str(e)}"
        print(error_msg)  # Log to console
        
        # Check auto data availability for template
        auto_data_available = all([
            Path("pse_processed_auto/afrr_prices.csv").exists(),
            Path("pse_processed_auto/afrr_volumes.csv").exists()
        ])
        
        return templates.TemplateResponse("index.html", {
            "request": request,
            "error": error_msg,
            "summary": None, 
            "chart": None,
            "download_files": None,
            "auto_data_available": auto_data_available,
            "fetch_status": fetch_status
        })

@app.post("/run-two-bids", response_class=HTMLResponse)
async def run_two_bids_analysis(
    request: Request,
    # Capacity parameters
    portfolio_mw: float = Form(50.0, description="Portfolio size (MW)"),
    cap_bidding_mode: str = Form("single", description="Capacity bidding: single or banded"),
    single_K_cap: Optional[float] = Form(250.0, description="Single capacity bid (PLN/MW-h)"),
    cap_bands_input: str = Form("", description="Capacity bands: v1:K1,v2:K2,..."),
    
    # Energy parameters  
    single_K_energy: Optional[float] = Form(-50.0, description="Energy bid (PLN/MWh)"),
    energy_pay_rule: str = Form("difference", description="Energy pay rule"),
    theta: float = Form(0.01, description="Activation probability"),
    
    # Model parameters
    cap_model: str = Form("parametric", description="Capacity model: parametric or empirical"), 
    ridge_lambda: float = Form(1.0, description="Ridge regularization parameter"),
    pred_cols_input: str = Form("", description="Predictor columns (comma-separated)"),
    
    # Output parameters
    out_prefix: str = Form("two_bids", description="Output file prefix"),
    return_intervals: bool = Form(True, description="Return interval-level data")
):
    """Run two-bid aFRR-down valuation analysis."""
    
    try:
        # Load automatically fetched PSE data
        try:
            prices_df, vols_df, preds_df = load_auto_data()
        except ValueError as e:
            auto_data_available = False
            return templates.TemplateResponse("two_bids.html", {
                "request": request,
                "error": f"Auto data not available: {str(e)}. Please fetch data first.",
                "summary": None,
                "chart": None, 
                "download_files": None,
                "auto_data_available": auto_data_available,
                "fetch_status": fetch_status
            })
        
        # Parse capacity bidding parameters
        cap_bands = None
        single_K_cap_param = None
        
        if cap_bidding_mode == "single":
            single_K_cap_param = single_K_cap
        elif cap_bidding_mode == "banded" and cap_bands_input.strip():
            try:
                cap_bands = []
                for band in cap_bands_input.split(','):
                    band = band.strip()
                    if ':' in band:
                        fraction_str, bid_str = band.split(':', 1)
                        cap_bands.append((float(fraction_str), float(bid_str)))
                if not cap_bands:
                    single_K_cap_param = single_K_cap  # Fallback
            except ValueError:
                single_K_cap_param = single_K_cap  # Fallback on parse error
        else:
            single_K_cap_param = single_K_cap  # Fallback
        
        # Parse predictor columns
        pred_cols_list = []
        if pred_cols_input.strip() and preds_df is not None:
            try:
                pred_cols_list = [col.strip() for col in pred_cols_input.split(',') 
                                if col.strip() in preds_df.columns]
            except:
                pred_cols_list = []
        
        # Load energy data if available
        energy_df = None
        energy_file = Path("synthetic_energy_prices.csv")
        if energy_file.exists():
            energy_df = pd.read_csv(energy_file)
        
        # Ensure all form parameters are properly typed and not undefined
        energy_pay_rule_safe = str(energy_pay_rule) if energy_pay_rule else "difference"
        theta_safe = float(theta) if theta is not None else 0.01
        single_K_energy_safe = float(single_K_energy) if single_K_energy is not None else 0.0
        
        # Run two-bid valuation with debugging
        try:
            results = value_afrr_down_two_bids(
                # Capacity data
                df_cap=prices_df, dt_cap="dt", col_cap="price_pln_per_mw_h", cap_unit="per_mw_h",
                df_vol=vols_df, dt_vol="dt", col_vol_mw="mw_procured",
                
                # Predictors (optional)
                df_pred=preds_df, pred_cols=pred_cols_list if pred_cols_list else None,
                ridge_lambda=ridge_lambda, cap_model=cap_model,
                
                # Capacity bids
                portfolio_mw=portfolio_mw, cap_bands=cap_bands, single_K_cap=single_K_cap_param,
                
                # Energy bid
                df_energy=energy_df, single_K_energy=single_K_energy_safe, 
                energy_pay_rule=energy_pay_rule_safe, theta=theta_safe,
                
                return_intervals=return_intervals
            )
        except Exception as e:
            print(f"Error in value_afrr_down_two_bids: {e}")
            print(f"Parameters: portfolio_mw={portfolio_mw}, cap_model={cap_model}")
            print(f"Energy params: K_energy={single_K_energy_safe}, pay_rule={energy_pay_rule_safe}, theta={theta_safe}")
            raise
        
        # Save outputs and create download links - temporarily disabled for debugging
        download_files = {}
        try:
            download_files = save_outputs(results, out_prefix)
        except Exception as e:
            print(f"Error in save_outputs: {e}")
            print(f"Results keys: {list(results.keys())}")
            # Continue without download files
        
        # Check auto data availability for template
        auto_data_available = all([
            Path("pse_processed_auto/afrr_prices.csv").exists(),
            Path("pse_processed_auto/afrr_volumes.csv").exists()
        ])
        
        return templates.TemplateResponse("two_bids.html", {
            "request": request,
            "summary": results,
            "chart": results.get('chart_data', {}),
            "download_files": download_files,
            "auto_data_available": auto_data_available,
            "fetch_status": fetch_status,
            "params": {
                "portfolio_mw": portfolio_mw,
                "cap_bidding_mode": cap_bidding_mode,
                "single_K_cap": single_K_cap_param or single_K_cap,
                "cap_bands_input": cap_bands_input,
                "single_K_energy": single_K_energy,
                "energy_pay_rule": energy_pay_rule,
                "theta": theta,
                "cap_model": cap_model,
                "ridge_lambda": ridge_lambda,
                "pred_cols_input": pred_cols_input,
                "out_prefix": out_prefix
            }
        })
        
    except Exception as e:
        error_msg = f"Two-bid analysis failed: {str(e)}"
        print(error_msg)  # Log to console
        
        # Check auto data availability for template
        auto_data_available = all([
            Path("pse_processed_auto/afrr_prices.csv").exists(),
            Path("pse_processed_auto/afrr_volumes.csv").exists()
        ])
        
        return templates.TemplateResponse("two_bids.html", {
            "request": request,
            "error": error_msg,
            "summary": None,
            "chart": None,
            "download_files": None,
            "auto_data_available": auto_data_available,
            "fetch_status": fetch_status
        })

@app.get("/two-bids", response_class=HTMLResponse)
async def two_bids_page(request: Request):
    """Display the two-bid analysis page."""
    # Check if auto data exists
    auto_data_available = all([
        Path("pse_processed_auto/afrr_prices.csv").exists(),
        Path("pse_processed_auto/afrr_volumes.csv").exists()
    ])
    
    return templates.TemplateResponse("two_bids.html", {
        "request": request,
        "summary": None,
        "chart": None,
        "download_files": None,
        "auto_data_available": auto_data_available,
        "fetch_status": fetch_status
    })

@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy", "service": "aFRR-down Black-Scholes Valuation"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=True)
