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

from valuation_core import run_pipeline

app = FastAPI(title="aFRR-down Black-Scholes Valuation", version="1.0.0")

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
            raise Exception(f"Data download failed: {result.stderr}")
        
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
            '--raw-dir', 'pse_data_js',
            '--output-dir', 'pse_processed_auto'
        ], cwd=Path.cwd(), check=True)
        
        # Also process direct aFRR data if available
        try:
            subprocess.run([
                'python3', 'process_pse_data.py',
                '--raw-dir', 'pse_raw_auto', 
                '--output-dir', 'pse_processed_auto'
            ], cwd=Path.cwd(), check=False)  # Don't fail if this doesn't work
        except:
            pass
        
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

def save_outputs(df: pd.DataFrame, summary: dict, prefix: str = "valuation") -> dict:
    """Save analysis outputs to files and return download links."""
    output_files = {}
    
    try:
        # Save intervals CSV
        intervals_file = f"outputs/{prefix}_intervals.csv"
        df.to_csv(intervals_file, index=False)
        output_files["intervals"] = {"name": "Intervals Data (CSV)", "path": f"/outputs/{prefix}_intervals.csv"}
        
        # Save summary JSON  
        summary_file = f"outputs/{prefix}_summary.json"
        with open(summary_file, 'w') as f:
            json.dump(summary, f, indent=2, default=str)
        output_files["summary"] = {"name": "Summary (JSON)", "path": f"/outputs/{prefix}_summary.json"}
        
        # Create daily aggregates if we have enough data
        if len(df) > 24:
            df_copy = df.copy()
            df_copy['date'] = pd.to_datetime(df_copy['dt']).dt.date
            
            daily_agg = df_copy.groupby('date').agg({
                'price_pln_per_mw_h': ['mean', 'min', 'max'],
                'mw_procured': 'mean',
                'budget_pln_interval': 'sum'
            }).round(2)
            
            # Add mode-specific columns if they exist
            for mode_prefix in ['exante', 'expost']:
                pln_col = f"{mode_prefix}_pln_portfolio_capped"
                p_accept_col = f"{mode_prefix}_p_accept"
                
                if pln_col in df_copy.columns:
                    daily_agg[(pln_col, 'sum')] = df_copy.groupby('date')[pln_col].sum()
                if p_accept_col in df_copy.columns:
                    daily_agg[(p_accept_col, 'mean')] = df_copy.groupby('date')[p_accept_col].mean()
            
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
    # Parameters
    K: float = Form(400.0, description="Strike price (PLN/MW-h)"),
    portfolio_mw: float = Form(50.0, description="Portfolio size (MW)"),
    ridge: float = Form(1.0, description="Ridge regularization parameter"),
    mode: str = Form("exante", description="Analysis mode: exante, expost, both"),
    out_prefix: str = Form("valuation", description="Output file prefix"),
    use_auto_data: bool = Form(True, description="Use automatically fetched PSE data")
):
    """Run complete aFRR-down Black-Scholes valuation analysis."""
    
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
        
        # Run analysis pipeline
        intervals_df, summary, chart_data = run_pipeline(
            prices_df, prices_dt_col, prices_col, prices_unit,
            vols_df, vols_dt_col, vols_mw_col,
            preds_df, pred_dt_col, pred_cols_list,
            K, portfolio_mw, ridge, mode
        )
        
        # Save outputs and create download links
        download_files = save_outputs(intervals_df, summary, out_prefix)
        
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
                "K": K, 
                "portfolio_mw": portfolio_mw, 
                "mode": mode, 
                "ridge": ridge,
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

@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy", "service": "aFRR-down Black-Scholes Valuation"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=True)
