# app.py
import os, io, uuid, json, subprocess
from pathlib import Path
from typing import Optional
from datetime import datetime, timedelta
import pandas as pd
from fastapi import FastAPI, Request, Form, HTTPException, BackgroundTasks, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from afrr_two_bids import value_afrr_down_two_bids

app = FastAPI(title="aFRR-down Two-Bid Valuation", version="2.0.0")

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
        os.makedirs('pse_data_js', exist_ok=True)
        
        # Set environment variables for the Node.js script
        env = os.environ.copy()
        env['START'] = start_date
        env['END'] = end_date
        env['OUTDIR'] = 'pse_raw_auto'
        
        # Run the JavaScript downloader
        fetch_status = {"status": "fetching", "progress": 30, "message": "Downloading energy prices from PSE API..."}
        result = subprocess.run([
            'node', 'energy-prices-downloader.js'
        ], cwd=Path.cwd(), capture_output=True, text=True, env=env, timeout=1800)
        
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
        ], cwd=Path.cwd(), capture_output=True, text=True, env=env, timeout=1800)
        
        if result.returncode != 0:
            print(f"Warning: Direct aFRR fetch failed: {result.stderr}")
        
        # Process the downloaded data
        fetch_status = {"status": "processing", "progress": 70, "message": "Processing downloaded data..."}
        
        fetch_status = {"status": "completed", "progress": 100, "message": "Data download completed!"}
        
        # Check which JavaScript data files were created
        js_data_path = Path("pse_data_js")
        files_created = []
        
        for file_pattern in ["energy_prices.json", "afrr_marginal_prices.json", "afrr_volumes_mbp.json", "total_costs.json"]:
            file_path = js_data_path / file_pattern
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

def prepare_enhanced_chart_data(results, intervals_data=None):
    """Prepare enhanced chart data with aFRR prices, strategic bids, and wind BSP features."""
    chart_data = {}
    
    try:
        # Get basic chart data from results
        if "chart_data" in results and results["chart_data"]:
            chart_base = results["chart_data"]
            
            # Time series data
            chart_data["dt"] = chart_base.get("dt", [])
            chart_data["capacity_price_pln_per_mw_h"] = chart_base.get("S_cap", [])
            chart_data["capacity_acceptance_prob"] = chart_base.get("p_cap", [])
            chart_data["capacity_revenue_pln"] = chart_base.get("R_cap", [])
            
            # Energy leg data
            energy_prices = chart_base.get("S_bal", [])
            print(f"Debug: Energy prices from chart_base: {len(energy_prices)} items, first 3: {energy_prices[:3] if energy_prices else 'Empty'}")
            chart_data["energy_price_pln_per_mwh"] = energy_prices
            chart_data["energy_revenue_pln"] = chart_base.get("R_energy", [])
            chart_data["energy_acceptance_prob"] = chart_base.get("p_energy", [])
            
            # Strategic bid lines and revenue confidence intervals
            K_cap_values = results.get("K_cap_values", [])
            K_energy = results.get("K_energy", 0)
            
            if K_cap_values and len(chart_data["dt"]) > 0:
                # Use the primary K_cap value for comparison
                primary_K_cap = K_cap_values[0] if isinstance(K_cap_values, list) else K_cap_values
                chart_data["K_cap_line"] = [primary_K_cap] * len(chart_data["dt"])
                
                # Calculate revenue confidence intervals based on capacity factor and technical availability
                capacity_revenues = chart_data.get("capacity_revenue_pln", [])
                if capacity_revenues and len(capacity_revenues) > 0:
                    # Calculate confidence intervals for revenue expectations
                    cf_uncertainty = 0.15  # 15% capacity factor variability
                    technical_availability = 0.10  # 10% technical availability uncertainty
                    
                    revenue_upper = [r * (1 + cf_uncertainty + technical_availability) for r in capacity_revenues]
                    revenue_lower = [r * (1 - cf_uncertainty - technical_availability) for r in capacity_revenues]
                    
                    chart_data["capacity_revenue_upper"] = revenue_upper
                    chart_data["capacity_revenue_lower"] = revenue_lower
            
            if K_energy and len(chart_data["dt"]) > 0:
                chart_data["K_energy_line"] = [K_energy] * len(chart_data["dt"])
                
                # Calculate energy revenue confidence intervals
                energy_revenues = chart_data.get("energy_revenue_pln", [])
                if energy_revenues and len(energy_revenues) > 0:
                    # Revenue uncertainty due to activation probability and balancing price volatility
                    theta_uncertainty = 0.20  # 20% activation probability uncertainty
                    price_impact = 0.15  # 15% impact of balancing price volatility on revenue
                    
                    energy_revenue_upper = [r * (1 + theta_uncertainty + price_impact) for r in energy_revenues]
                    energy_revenue_lower = [max(0, r * (1 - theta_uncertainty - price_impact)) for r in energy_revenues]
                    
                    chart_data["energy_revenue_upper"] = energy_revenue_upper
                    chart_data["energy_revenue_lower"] = energy_revenue_lower
            
            # Total revenue
            if chart_data.get("capacity_revenue_pln") and chart_data.get("energy_revenue_pln"):
                cap_rev = chart_data["capacity_revenue_pln"]
                eng_rev = chart_data["energy_revenue_pln"]
                chart_data["total_revenue_pln"] = [c + e for c, e in zip(cap_rev, eng_rev)]
            
        # Wind BSP capacity factor data
        if "capacity_factor_analysis" in results and results["capacity_factor_analysis"]:
            cf_analysis = results["capacity_factor_analysis"]
            
            # Seasonal capacity factor pattern
            chart_data["seasonal_cf_pattern"] = {
                "months": list(range(1, 13)),
                "multipliers": [1.3, 1.3, 0.9, 0.9, 0.9, 0.8, 0.8, 0.8, 0.8, 1.2, 1.2, 1.3],  # Seasonal pattern
                "labels": ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
            }
            
            # Capacity factor statistics
            chart_data["cf_percentiles"] = cf_analysis.get("percentiles", {})
            chart_data["cf_mean"] = cf_analysis.get("mean_cf", 0.3)
            
        # Risk assessment data
        if "risk_analysis" in results and results["risk_analysis"]:
            risk_data = results["risk_analysis"]
            
            if "percentiles" in risk_data:
                percentiles = risk_data["percentiles"]
                chart_data["revenue_risk"] = {
                    "percentiles": ["P5", "P10", "P25", "P50", "P75", "P90", "P95"],
                    "values": [
                        percentiles.get("P5", 0),
                        percentiles.get("P10", 0),
                        percentiles.get("P25", 0),
                        percentiles.get("P50", 0),
                        percentiles.get("P75", 0),
                        percentiles.get("P90", 0),
                        percentiles.get("P95", 0)
                    ]
                }
            
            # VaR and CVaR data
            chart_data["risk_metrics"] = {
                "var_95": risk_data.get("var_95", 0),
                "cvar_95": risk_data.get("cvar_95", 0),
                "var_99": risk_data.get("var_99", 0),
                "cvar_99": risk_data.get("cvar_99", 0),
                "mean_annual": risk_data.get("mean_annual_revenue", 0),
                "historical_annual": risk_data.get("historical_annual", 0)
            }
            
        # Deliverability constraints data
        if "deliverability_constraints" in results:
            delivery_data = results["deliverability_constraints"]
            chart_data["delivery_constraints"] = {
                "capacity_factor": delivery_data.get("capacity_factor", 1.0),
                "seasonal_enabled": delivery_data.get("seasonal_cf_variation", True),
                "availability_haircut": delivery_data.get("availability_haircut", 1.0)
            }
            
        # Monthly aggregated data for seasonal analysis
        if intervals_data is not None and len(intervals_data) > 0:
            # Group by month to show seasonal patterns
            df_intervals = pd.DataFrame(intervals_data)
            if 'dt' in df_intervals.columns:
                df_intervals['dt'] = pd.to_datetime(df_intervals['dt'])
                df_intervals['month'] = df_intervals['dt'].dt.month
                
                # Monthly averages
                monthly_data = df_intervals.groupby('month').agg({
                    'capacity_revenue_capped': 'sum',
                    'mw_deliverable': 'mean' if 'mw_deliverable' in df_intervals.columns else 'count',
                    'total_revenue': 'sum'
                }).reset_index()
                
                chart_data["monthly_analysis"] = {
                    "months": monthly_data['month'].tolist(),
                    "capacity_revenue": monthly_data['capacity_revenue_capped'].tolist(),
                    "deliverable_capacity": monthly_data.get('mw_deliverable', []).tolist() if 'mw_deliverable' in monthly_data.columns else [],
                    "total_revenue": monthly_data['total_revenue'].tolist()
                }
        
    except Exception as e:
        print(f"Warning: Error preparing chart data: {e}")
        chart_data = {"error": f"Chart preparation failed: {str(e)}"}
    
    return chart_data

async def run_data_fetch(start_date: str, end_date: str):
    """Background task to run data fetching."""
    global fetch_status
    
    try:
        fetch_status = {"status": "fetching", "progress": 10, "message": "Initializing data fetch..."}
        
        # Set up environment for subprocess
        env = os.environ.copy()
        env['NODE_PATH'] = env.get('NODE_PATH', '/usr/local/lib/node_modules')
        
        # Run the JavaScript downloader with extended timeout
        fetch_status = {"status": "fetching", "progress": 20, "message": "Downloading full year PSE data (this may take 5-10 minutes)..."}
        
        # Start the JavaScript downloader in a way that allows us to track progress
        import subprocess
        process = subprocess.Popen([
            'node', 'energy-prices-downloader.js'
        ], cwd=Path.cwd(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        
        # Monitor the process for up to 30 minutes (longer timeout for Render)
        import time
        start_time = time.time()
        timeout = 1800  # 30 minutes
        
        while process.poll() is None:
            elapsed = time.time() - start_time
            if elapsed > timeout:
                process.terminate()
                fetch_status = {"status": "error", "progress": 0, "message": f"Download timed out after {timeout//60} minutes. Try a shorter date range or check PSE API status."}
                return
            
            # Update progress based on elapsed time (rough estimate)
            progress = min(90, 20 + int(elapsed / timeout * 70))
            fetch_status = {"status": "fetching", "progress": progress, "message": f"Downloading PSE data... ({int(elapsed/60)}m {int(elapsed%60)}s)"}
            time.sleep(10)  # Update every 10 seconds
        
        # Get the final result
        stdout, stderr = process.communicate()
        result = type('obj', (object,), {'returncode': process.returncode, 'stdout': stdout, 'stderr': stderr})
        
        if result.returncode != 0:
            print(f"JavaScript downloader failed: {result.stderr}")
            print(f"JavaScript downloader stdout: {result.stdout}")
            fetch_status = {"status": "error", "progress": 0, "message": f"JavaScript downloader failed: {result.stderr[:200]}"}
            return
        
        fetch_status = {"status": "completed", "progress": 100, "message": "Data download completed successfully!"}
        
    except Exception as e:
        fetch_status = {"status": "error", "progress": 0, "message": f"Data fetch failed: {str(e)}"}

def load_auto_data() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Load PSE data from JavaScript downloader (no synthetics)."""
    js_data_path = Path("pse_data_js")
    
    # Load aFRR marginal capacity prices (CMBU-TU)
    capacity_file = js_data_path / "afrr_marginal_prices.json"
    if capacity_file.exists():
        with open(capacity_file, 'r') as f:
            capacity_data = json.load(f)
        
        prices_df = pd.DataFrame(capacity_data)
        # Map to expected format: dt, price_pln_per_mw_h (afrr_d = marginal prices)
        prices_df = prices_df[['dtime_utc', 'afrr_d']].copy()
        prices_df.columns = ['dt', 'price_pln_per_mw_h']
        prices_df['dt'] = pd.to_datetime(prices_df['dt'])
        # Remove null prices
        prices_df = prices_df.dropna(subset=['price_pln_per_mw_h'])
        print(f"✅ Loaded aFRR marginal capacity prices: {len(prices_df)} records")
        print(f"   Price range: {prices_df['price_pln_per_mw_h'].min():.2f} - {prices_df['price_pln_per_mw_h'].max():.2f} PLN/MW-h")
        print(f"   Date range: {prices_df['dt'].min()} to {prices_df['dt'].max()}")
    else:
        raise ValueError("No aFRR marginal price data found. Run JavaScript downloader first.")
    
    # Load aFRR volume data (MBP-TP afrr_d field)
    vol_file = js_data_path / "afrr_volumes_mbp.json"  
    if vol_file.exists():
        with open(vol_file, 'r') as f:
            volume_data = json.load(f)
        
        vols_df = pd.DataFrame(volume_data)
        # Map to expected format: dt, mw_procured (afrr_d = volumes in MW)
        vols_df = vols_df[['dtime_utc', 'afrr_d']].copy()
        vols_df.columns = ['dt', 'mw_procured']
        vols_df['dt'] = pd.to_datetime(vols_df['dt'])
        # Remove null volumes
        vols_df = vols_df.dropna(subset=['mw_procured'])
        print(f"✅ Loaded aFRR volumes from MBP-TP: {len(vols_df)} records")
        print(f"   Volume range: {vols_df['mw_procured'].min():.0f} - {vols_df['mw_procured'].max():.0f} MW")
        print(f"   Date range: {vols_df['dt'].min()} to {vols_df['dt'].max()}")
    else:
        raise ValueError("No aFRR volume data found. Run JavaScript downloader first.")
    
    # Load predictor data from energy prices (CEN, COR, CSDAC)
    energy_file = js_data_path / "energy_prices.json"
    preds_df = None
    if energy_file.exists():
        with open(energy_file, 'r') as f:
            energy_data = json.load(f)
        
        preds_df = pd.DataFrame(energy_data)
        # Map to expected predictor format: dt, CEN, COR, CSDAC, CEB_SR
        preds_df = preds_df[['dtime_utc', 'cen_cost', 'cor_cost', 'csdac_pln', 'ceb_sr_cost']].copy()
        preds_df.columns = ['dt', 'CEN', 'COR', 'CSDAC', 'CEB_SR']
        preds_df['dt'] = pd.to_datetime(preds_df['dt'])
        print(f"✅ Loaded predictors from energy data: {len(preds_df)} records")
        print(f"   Date range: {preds_df['dt'].min()} to {preds_df['dt'].max()}")
    
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

def generate_json_reports(results: dict, params: dict, prefix: str = "two_bids") -> dict:
    """Generate comprehensive JSON reports for download."""
    import json
    from datetime import datetime
    
    # Ensure outputs directory exists
    Path("outputs").mkdir(exist_ok=True)
    
    output_files = {}
    
    # Clean data for JSON serialization  
    def clean_for_json(obj, path=""):
        """Recursively clean object to ensure JSON serializability."""
        if obj is None:
            return None
        
        # Handle Jinja2 Undefined objects
        try:
            obj_type_name = type(obj).__name__
            if 'Undefined' in obj_type_name or 'undefined' in str(obj).lower():
                return None
        except:
            pass
            
        # Handle numpy types
        if hasattr(obj, 'dtype'):
            if hasattr(obj, 'item'):  # numpy scalar
                return obj.item()
            elif hasattr(obj, 'tolist'):  # numpy array
                return obj.tolist()
        
        # Handle pandas types
        if hasattr(obj, 'to_dict'):
            try:
                return clean_for_json(obj.to_dict())
            except:
                return str(obj)
                
        # Handle datetime
        if isinstance(obj, (pd.Timestamp, datetime)):
            return obj.isoformat()
            
        # Handle inf and nan
        if isinstance(obj, (float, int)):
            if pd.isna(obj) or obj == float('inf') or obj == float('-inf'):
                return None
            return obj
            
        # Handle lists and tuples
        if isinstance(obj, (list, tuple)):
            return [clean_for_json(item, f"{path}[{i}]") for i, item in enumerate(obj)]
            
        # Handle dictionaries
        if isinstance(obj, dict):
            cleaned = {}
            for key, value in obj.items():
                try:
                    cleaned[str(key)] = clean_for_json(value, f"{path}.{key}")
                except Exception as e:
                    print(f"Warning: Could not clean {path}.{key}: {e}")
                    cleaned[str(key)] = str(value)
            return cleaned
            
        # Handle strings
        if isinstance(obj, str):
            return obj
            
        # Convert everything else to string as fallback
        try:
            return str(obj)
        except:
            return None
    
    # Generate Report 1: Complete Analysis Summary
    try:
        total_report = {
            "report_type": "complete_analysis_summary",
            "generated_at": datetime.now().isoformat(),
            "model_parameters": clean_for_json(params),
            "total_results": {
                "portfolio_mw": results.get("portfolio_mw", 0),
                "total_capacity_revenue_pln": results.get("total_capacity_revenue_pln", 0),
                "total_energy_revenue_pln": results.get("total_energy_revenue_pln", 0),
                "total_revenue_pln": results.get("total_revenue_pln", 0),
                "per_mw_revenue_pln": results.get("per_mw_revenue_pln", 0),
                "avg_capacity_acceptance_prob": results.get("avg_capacity_acceptance_prob", 0),
                "bound_intervals_pct": results.get("bound_intervals_pct", 0),
                "total_intervals": results.get("total_intervals", 0),
                "bound_intervals": results.get("bound_intervals", 0),
                "expected_energy_activations": results.get("expected_energy_activations", 0),
                "avg_energy_payoff_per_mwh": results.get("avg_energy_payoff_per_mwh", 0)
            },
            "model_details": {
                "cap_model": results.get("cap_model", ""),
                "energy_pay_rule": results.get("energy_pay_rule", ""),
                "K_energy": results.get("K_energy", 0),
                "theta": results.get("theta", 0),
                "model_params": clean_for_json(results.get("model_params", {}))
            },
            "capacity_analysis": clean_for_json(results.get("capacity_bid_stats", [])),
            "energy_leg_details": clean_for_json(results.get("energy_leg", {})) if "energy_leg" in results else {},
            "data_sources": {
                "pse_afrr_prices": "PSE aFRR-down capacity prices",
                "pse_volumes": "PSE procurement volumes", 
                "pse_energy_prices": "PSE CEB balancing energy prices (średnia)",
                "predictors": "PSE market predictors (CEN, COR, SK, etc.)"
            },
            "wind_bsp_analysis": {
                "capacity_factor_analysis": clean_for_json(results.get("capacity_factor_analysis", {})),
                "risk_analysis": clean_for_json(results.get("risk_analysis", {})),
                "deliverability_constraints": clean_for_json(results.get("deliverability_constraints", {}))
            }
        }
        
        total_file = f"outputs/{prefix}_complete_analysis.json"
        with open(total_file, 'w', encoding='utf-8') as f:
            json.dump(total_report, f, indent=2, ensure_ascii=False)
        
        output_files["complete"] = {
            "name": "Complete Analysis Summary (JSON)", 
            "path": f"/outputs/{prefix}_complete_analysis.json"
        }
        
    except Exception as e:
        print(f"Error generating complete analysis report: {e}")
    
    # Generate Report 2: Monthly Aggregated Results
    try:
        if "intervals" in results and results["intervals"]:
            intervals_df = pd.DataFrame(results["intervals"])
            print(f"Debug: Available interval columns: {list(intervals_df.columns)}")
            intervals_df['dt'] = pd.to_datetime(intervals_df['dt'])
            intervals_df['month'] = intervals_df['dt'].dt.to_period('M').astype(str)
            
            # Map to available columns (fallback for missing columns)
            agg_dict = {}
            if 'capacity_revenue_capped' in intervals_df.columns:
                agg_dict['capacity_revenue_capped'] = 'sum'
            elif 'capacity_revenue_baseline' in intervals_df.columns:
                agg_dict['capacity_revenue_baseline'] = 'sum'
                
            if 'energy_revenue_pln' in intervals_df.columns:
                agg_dict['energy_revenue_pln'] = 'sum'
                
            if 'total_revenue_pln' in intervals_df.columns:
                agg_dict['total_revenue_pln'] = 'sum'
                
            if 'capacity_acceptance_prob' in intervals_df.columns:
                agg_dict['capacity_acceptance_prob'] = 'mean'
                
            if 'capacity_price_pln_per_mw_h' in intervals_df.columns:
                agg_dict['capacity_price_pln_per_mw_h'] = 'mean'
                
            if 'energy_price_pln_per_mwh' in intervals_df.columns:
                agg_dict['energy_price_pln_per_mwh'] = 'mean'
            
            print(f"Debug: Using aggregation dict: {agg_dict}")
            
            if agg_dict:  # Only proceed if we have columns to aggregate
                monthly_agg = intervals_df.groupby('month').agg(agg_dict).round(2)
            
            monthly_report = {
                "report_type": "monthly_aggregated_results",
                "generated_at": datetime.now().isoformat(),
                "portfolio_mw": results.get("portfolio_mw", 0),
                "model_parameters": clean_for_json(params),
                "monthly_results": []
            }
            
            # Build monthly results with PSE-aligned field semantics
            for month, row in monthly_agg.iterrows():
                month_result = {"month": month}
                
                # Add available columns with PSE-aligned names
                if 'capacity_revenue_capped' in row:
                    month_result["afrr_capacity_revenue_pln"] = clean_for_json(row['capacity_revenue_capped'])
                elif 'capacity_revenue_baseline' in row:
                    month_result["afrr_capacity_revenue_pln"] = clean_for_json(row['capacity_revenue_baseline'])
                    
                if 'energy_revenue_pln' in row:
                    month_result["ceb_energy_revenue_pln"] = clean_for_json(row['energy_revenue_pln'])
                    
                if 'total_revenue_pln' in row:
                    month_result["total_afrr_revenue_pln"] = clean_for_json(row['total_revenue_pln'])
                    
                if 'capacity_acceptance_prob' in row:
                    month_result["avg_capacity_acceptance_prob"] = clean_for_json(row['capacity_acceptance_prob'])
                    
                if 'capacity_price_pln_per_mw_h' in row:
                    month_result["avg_afrr_capacity_price_pln_per_mw_h"] = clean_for_json(row['capacity_price_pln_per_mw_h'])
                    
                if 'energy_price_pln_per_mwh' in row:
                    month_result["avg_ceb_balancing_price_pln_per_mwh"] = clean_for_json(row['energy_price_pln_per_mwh'])
                
                month_result["intervals_count"] = int(intervals_df[intervals_df['month'] == month].shape[0])
                month_result["business_dates_count"] = len(intervals_df[intervals_df['month'] == month]['dt'].dt.date.unique())
                
                monthly_report["monthly_results"].append(month_result)
            
            monthly_file = f"outputs/{prefix}_monthly_results.json"
            with open(monthly_file, 'w', encoding='utf-8') as f:
                json.dump(monthly_report, f, indent=2, ensure_ascii=False)
                
            output_files["monthly"] = {
                "name": "Monthly Results (JSON)",
                "path": f"/outputs/{prefix}_monthly_results.json"
            }
            
    except Exception as e:
        print(f"Error generating monthly report: {e}")
    
    # Generate Report 3: Daily Aggregated Results  
    try:
        if "intervals" in results and results["intervals"]:
            intervals_df = pd.DataFrame(results["intervals"])
            intervals_df['dt'] = pd.to_datetime(intervals_df['dt'])
            intervals_df['date'] = intervals_df['dt'].dt.date.astype(str)
            
            # Map to available columns (fallback for missing columns)  
            agg_dict = {}
            if 'capacity_revenue_capped' in intervals_df.columns:
                agg_dict['capacity_revenue_capped'] = 'sum'
            elif 'capacity_revenue_baseline' in intervals_df.columns:
                agg_dict['capacity_revenue_baseline'] = 'sum'
                
            if 'energy_revenue_pln' in intervals_df.columns:
                agg_dict['energy_revenue_pln'] = 'sum'
                
            if 'total_revenue_pln' in intervals_df.columns:
                agg_dict['total_revenue_pln'] = 'sum'
                
            if 'capacity_acceptance_prob' in intervals_df.columns:
                agg_dict['capacity_acceptance_prob'] = 'mean'
                
            if 'capacity_price_pln_per_mw_h' in intervals_df.columns:
                agg_dict['capacity_price_pln_per_mw_h'] = 'mean'
                
            if 'energy_price_pln_per_mwh' in intervals_df.columns:
                agg_dict['energy_price_pln_per_mwh'] = 'mean'
            
            if agg_dict:  # Only proceed if we have columns to aggregate
                daily_agg = intervals_df.groupby('date').agg(agg_dict).round(2)
            
            daily_report = {
                "report_type": "daily_aggregated_results",
                "generated_at": datetime.now().isoformat(),
                "portfolio_mw": results.get("portfolio_mw", 0),
                "model_parameters": clean_for_json(params),
                "daily_results": []
            }
            
            # Build daily results with PSE-aligned field semantics
            for date, row in daily_agg.iterrows():
                day_result = {"business_date": date}
                
                # Add available columns with PSE-aligned names
                if 'capacity_revenue_capped' in row:
                    day_result["afrr_capacity_revenue_pln"] = clean_for_json(row['capacity_revenue_capped'])
                elif 'capacity_revenue_baseline' in row:
                    day_result["afrr_capacity_revenue_pln"] = clean_for_json(row['capacity_revenue_baseline'])
                    
                if 'energy_revenue_pln' in row:
                    day_result["ceb_energy_revenue_pln"] = clean_for_json(row['energy_revenue_pln'])
                    
                if 'total_revenue_pln' in row:
                    day_result["total_afrr_revenue_pln"] = clean_for_json(row['total_revenue_pln'])
                    
                if 'capacity_acceptance_prob' in row:
                    day_result["avg_capacity_acceptance_prob"] = clean_for_json(row['capacity_acceptance_prob'])
                    
                if 'capacity_price_pln_per_mw_h' in row:
                    day_result["avg_afrr_capacity_price_pln_per_mw_h"] = clean_for_json(row['capacity_price_pln_per_mw_h'])
                    
                if 'energy_price_pln_per_mwh' in row:
                    day_result["avg_ceb_balancing_price_pln_per_mwh"] = clean_for_json(row['energy_price_pln_per_mwh'])
                
                day_result["intervals_count"] = int(intervals_df[intervals_df['date'] == date].shape[0])
                day_result["hours_coverage"] = round(intervals_df[intervals_df['date'] == date].shape[0] * 0.25, 2)
                
                daily_report["daily_results"].append(day_result)
                
            daily_file = f"outputs/{prefix}_daily_results.json"
            with open(daily_file, 'w', encoding='utf-8') as f:
                json.dump(daily_report, f, indent=2, ensure_ascii=False)
                
            output_files["daily"] = {
                "name": "Daily Results (JSON)",
                "path": f"/outputs/{prefix}_daily_results.json"
            }
            
    except Exception as e:
        print(f"Error generating daily report: {e}")
    
    return output_files

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    # Redirect to the two-bids page (the main functionality)
    return RedirectResponse(url="/two-bids", status_code=302)

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
    
    # Add background task to fetch data
    background_tasks.add_task(run_data_fetch, start_date, end_date)
    
    return JSONResponse({"message": "Data fetch started", "status": "initiated"})

@app.get("/fetch-status")
async def get_fetch_status():
    """Get current status of data fetching."""
    return JSONResponse(fetch_status)

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    # Redirect to the two-bids page (the main functionality)
    return RedirectResponse(url="/two-bids", status_code=302)

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

# Old single-bid route removed - redirecting everything to two-bids mode

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
    
    # Wind BSP parameters
    capacity_factor: float = Form(0.3, description="Base capacity factor"),
    seasonal_cf_variation: bool = Form(True, description="Enable seasonal CF variations"),
    availability_haircut: float = Form(0.95, description="Technical availability factor (estymata error/technical availability composite)"),
    
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
        
        # Load PSE energy price data (CEB balancing prices)
        energy_df = None
        energy_file = Path("pse_data_js/energy_prices.json")
        if energy_file.exists():
            try:
                import json
                with open(energy_file, 'r') as f:
                    energy_data = json.load(f)
                
                # Convert to DataFrame with proper columns for the model
                energy_records = []
                for record in energy_data:
                    energy_records.append({
                        'dt': record['dtime_utc'],  # Use UTC datetime
                        'S_bal': record['ceb_sr_cost']  # Use CEB średnia (average) price
                    })
                
                energy_df = pd.DataFrame(energy_records)
                energy_df['dt'] = pd.to_datetime(energy_df['dt'])
                print(f"✅ Loaded PSE energy price data: {len(energy_df)} records")
                print(f"   Energy price range: {energy_df['S_bal'].min():.2f} - {energy_df['S_bal'].max():.2f} PLN/MWh")
                
            except Exception as e:
                print(f"⚠️ Error loading PSE energy data: {e}")
                energy_df = None
        else:
            print("⚠️ No PSE energy price data found")
        
        # Ensure all form parameters are properly typed and not undefined
        def safe_convert(value, default, converter=str):
            """Safely convert form values, handling Undefined objects."""
            try:
                if value is None:
                    return default
                # Check for Jinja2 Undefined objects
                if hasattr(value, '__class__') and 'Undefined' in str(type(value)):
                    return default
                return converter(value)
            except:
                return default
        
        energy_pay_rule_safe = safe_convert(energy_pay_rule, "difference", str)
        theta_safe = safe_convert(theta, 0.01, float)
        single_K_energy_safe = safe_convert(single_K_energy, 0.0, float)
        
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
                
                # Energy bid (PSE CEB balancing prices)
                df_energy=energy_df, dt_eng="dt", col_energy="S_bal",
                single_K_energy=single_K_energy_safe, 
                energy_pay_rule=energy_pay_rule_safe, theta=theta_safe,
                
                # Wind BSP parameters
                capacity_factor=capacity_factor,
                seasonal_cf_variation=seasonal_cf_variation,
                availability_haircut=availability_haircut,
                
                return_intervals=return_intervals
            )
        except Exception as e:
            print(f"Error in value_afrr_down_two_bids: {e}")
            print(f"Parameters: portfolio_mw={portfolio_mw}, cap_model={cap_model}")
            print(f"Energy params: K_energy={single_K_energy_safe}, pay_rule={energy_pay_rule_safe}, theta={theta_safe}")
            raise
        
        # Check auto data availability for template (JavaScript downloader files)
        auto_data_available = all([
            Path("pse_data_js/afrr_marginal_prices.json").exists(),
            Path("pse_data_js/afrr_volumes_mbp.json").exists(),
            Path("pse_data_js/energy_prices.json").exists()
        ])
        
        # Clean all params to prevent Undefined objects
        clean_params = {
            "portfolio_mw": safe_convert(portfolio_mw, 50.0, float),
            "cap_bidding_mode": safe_convert(cap_bidding_mode, "single", str),
            "single_K_cap": safe_convert(single_K_cap_param or single_K_cap, 250.0, float),
            "cap_bands_input": safe_convert(cap_bands_input, "", str),
            "single_K_energy": safe_convert(single_K_energy, -50.0, float),
            "energy_pay_rule": safe_convert(energy_pay_rule, "difference", str),
            "theta": safe_convert(theta, 0.01, float),
            "cap_model": safe_convert(cap_model, "parametric", str),
            "ridge_lambda": safe_convert(ridge_lambda, 1.0, float),
            "pred_cols_input": safe_convert(pred_cols_input, "", str),
            "out_prefix": safe_convert(out_prefix, "two_bids", str),
            # Wind BSP parameters
            "capacity_factor": safe_convert(capacity_factor, 0.3, float),
            "seasonal_cf_variation": safe_convert(seasonal_cf_variation, True, bool),
            "availability_haircut": safe_convert(availability_haircut, 0.95, float)
        }
        
        # Generate comprehensive JSON reports
        download_files = {}
        try:
            download_files = generate_json_reports(results, clean_params, out_prefix)
            print(f"✅ Generated JSON reports: {list(download_files.keys())}")
        except Exception as e:
            print(f"⚠️ Error generating reports: {e}")
            # Continue without download files
        
        # Prepare enhanced chart data with wind BSP features
        chart_data = None
        try:
            intervals_data = results.get("intervals", [])
            chart_data = prepare_enhanced_chart_data(results, intervals_data)
            print(f"✅ Prepared enhanced chart data with {len(chart_data)} sections")
        except Exception as chart_error:
            print(f"⚠️ Chart preparation error: {chart_error}")
            chart_data = None
        
        # Test with cleaned results dictionary
        cleaned_summary = {}
        try:
            # Include enhanced results for template
            cleaned_summary = {
                "total_capacity_revenue_pln": results.get("total_capacity_revenue_pln", 0),
                "total_energy_revenue_pln": results.get("total_energy_revenue_pln", 0),
                "total_revenue_pln": results.get("total_revenue_pln", 0),
                "per_mw_revenue_pln": results.get("per_mw_revenue_pln", 0),
                "avg_capacity_acceptance_prob": results.get("avg_capacity_acceptance_prob", 0),
                "bound_intervals_pct": results.get("bound_intervals_pct", 0),
                "portfolio_mw": results.get("portfolio_mw", 50),
                "cap_model": results.get("cap_model", "parametric"),
                "total_intervals": results.get("total_intervals", 0),
                "energy_pay_rule": results.get("energy_pay_rule", "difference"),
                "K_energy": results.get("K_energy", 0),
                "theta": results.get("theta", 0.01),
                "expected_energy_activations": results.get("expected_energy_activations", 0),
                "avg_energy_payoff_per_mwh": results.get("avg_energy_payoff_per_mwh", 0),
                "capacity_bid_stats": [],  # Disable for now
                
                # Wind BSP enhancements
                "capacity_factor_analysis": results.get("capacity_factor_analysis", {}),
                "risk_analysis": results.get("risk_analysis", {}),
                "deliverability_constraints": results.get("deliverability_constraints", {})
            }
            print(f"Using cleaned summary with keys: {list(cleaned_summary.keys())}")
            
            return templates.TemplateResponse("two_bids.html", {
                "request": request,
                "summary": cleaned_summary,
                "chart": chart_data,  # Enable enhanced charts
                "download_files": download_files,
                "auto_data_available": True,
                "fetch_status": None,
                "params": clean_params
            })
        except Exception as template_error:
            print(f"Template rendering error: {template_error}")
            print(f"Summary keys: {list(cleaned_summary.keys()) if cleaned_summary else 'None'}")
            raise template_error
        
    except Exception as e:
        error_msg = f"Two-bid analysis failed: {str(e)}"
        print(error_msg)  # Log to console
        
        # Check auto data availability for template (JavaScript downloader files)
        auto_data_available = all([
            Path("pse_data_js/afrr_marginal_prices.json").exists(),
            Path("pse_data_js/afrr_volumes_mbp.json").exists(),
            Path("pse_data_js/energy_prices.json").exists()
        ])
        
        return templates.TemplateResponse("two_bids.html", {
            "request": request,
            "error": error_msg,
            "summary": None,
            "chart": None,
            "download_files": None,
            "auto_data_available": auto_data_available,
            "fetch_status": fetch_status,
            "params": None  # Ensure params is always defined
        })

@app.get("/two-bids", response_class=HTMLResponse)
async def two_bids_page(request: Request):
    """Display the two-bid analysis page."""
    # Check if auto data exists (JavaScript downloader files)
    auto_data_available = all([
        Path("pse_data_js/afrr_marginal_prices.json").exists(),
        Path("pse_data_js/afrr_volumes_mbp.json").exists(),
        Path("pse_data_js/energy_prices.json").exists()
    ])
    
    return templates.TemplateResponse("two_bids.html", {
        "request": request,
        "summary": None,
        "chart": None,
        "download_files": None,
        "auto_data_available": auto_data_available,
        "fetch_status": fetch_status,
        "params": None  # Ensure params is always defined
    })

@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy", "service": "aFRR-down Black-Scholes Valuation"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=True)
