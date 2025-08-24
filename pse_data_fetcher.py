#!/usr/bin/env python3
"""
PSE Data Fetcher - Python replacement for energy-prices-downloader.js
Fetches aFRR capacity prices, volumes, and energy balancing prices from PSE API
"""
import json
import requests
import os
from datetime import datetime, timedelta
from pathlib import Path
import time

class PSEDataFetcher:
    def __init__(self):
        self.base_url = "https://www.pse.pl/getCSV"
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'en-US,en;q=0.5',
            'Referer': 'https://www.pse.pl/'
        }
        self.output_dir = Path("pse_data_js")
        self.output_dir.mkdir(exist_ok=True)
        
    def fetch_data_with_retry(self, url, params, max_retries=3):
        """Fetch data with retry logic for robustness"""
        for attempt in range(max_retries):
            try:
                print(f"Fetching data (attempt {attempt + 1}/{max_retries}): {params.get('c', 'unknown')}")
                response = requests.get(url, params=params, headers=self.headers, timeout=30)
                response.raise_for_status()
                return response.json()
            except Exception as e:
                print(f"Attempt {attempt + 1} failed: {e}")
                if attempt == max_retries - 1:
                    raise
                time.sleep(2 ** attempt)  # Exponential backoff
    
    def fetch_afrr_capacity_prices(self, start_date="2024-07-01", end_date="2025-06-30"):
        """Fetch aFRR-down capacity marginal prices"""
        params = {
            'c': 'cmbu-tu',
            'from': start_date,
            'to': end_date
        }
        
        try:
            data = self.fetch_data_with_retry(self.base_url, params)
            
            # Process and clean data
            processed_data = []
            for record in data:
                if 'afrrd' in record and record['afrrd'] is not None:
                    processed_data.append({
                        'dtime_utc': record.get('dtime_utc', record.get('dtime')),
                        'price_pln_per_mw_h': float(record['afrrd']),
                        'period': record.get('period', ''),
                        'business_date': record.get('business_date', '')
                    })
            
            # Save to file
            output_file = self.output_dir / "afrr_marginal_prices.json"
            with open(output_file, 'w') as f:
                json.dump(processed_data, f, indent=2)
            
            print(f"✅ Saved aFRR capacity prices: {len(processed_data)} records")
            return processed_data
            
        except Exception as e:
            print(f"❌ Failed to fetch aFRR capacity prices: {e}")
            raise
    
    def fetch_afrr_volumes(self, start_date="2024-07-01", end_date="2025-06-30"):
        """Fetch aFRR-down procurement volumes"""
        params = {
            'c': 'zmb',
            'from': start_date,
            'to': end_date
        }
        
        try:
            data = self.fetch_data_with_retry(self.base_url, params)
            
            # Process and clean data
            processed_data = []
            for record in data:
                if 'zmb_afrrd' in record and record['zmb_afrrd'] is not None:
                    processed_data.append({
                        'dtime_utc': record.get('dtime_utc', record.get('dtime')),
                        'mw_procured': float(record['zmb_afrrd']),
                        'period': record.get('period', ''),
                        'business_date': record.get('business_date', '')
                    })
            
            # Save to file
            output_file = self.output_dir / "afrr_volumes_mbp.json"
            with open(output_file, 'w') as f:
                json.dump(processed_data, f, indent=2)
            
            print(f"✅ Saved aFRR volumes: {len(processed_data)} records")
            return processed_data
            
        except Exception as e:
            print(f"❌ Failed to fetch aFRR volumes: {e}")
            raise
    
    def fetch_energy_prices(self, start_date="2024-07-01", end_date="2025-06-30"):
        """Fetch energy balancing prices and market predictors"""
        params = {
            'c': 'price-cost',
            'from': start_date,
            'to': end_date
        }
        
        try:
            data = self.fetch_data_with_retry(self.base_url, params)
            
            # Process and clean data
            processed_data = []
            for record in data:
                processed_record = {
                    'dtime_utc': record.get('dtime_utc', record.get('dtime')),
                    'period': record.get('period', ''),
                    'business_date': record.get('business_date', ''),
                    'balance': record.get('balance', 0),
                    'ceb_sr_cost': record.get('ceb_sr_cost', 0),
                    'ceb_pp_cost': record.get('ceb_pp_cost', 0),
                    # Market predictors
                    'cen_cost': record.get('cen_cost', 0),
                    'cor_cost': record.get('cor_cost', 0),
                    'sk_cost': record.get('sk_cost', 0),
                }
                processed_data.append(processed_record)
            
            # Save to file
            output_file = self.output_dir / "energy_prices.json"
            with open(output_file, 'w') as f:
                json.dump(processed_data, f, indent=2)
            
            print(f"✅ Saved energy prices: {len(processed_data)} records")
            return processed_data
            
        except Exception as e:
            print(f"❌ Failed to fetch energy prices: {e}")
            raise
    
    def fetch_all_data(self, start_date="2024-07-01", end_date="2025-06-30"):
        """Fetch all PSE data required for aFRR analysis"""
        print(f"🚀 Starting PSE data fetch from {start_date} to {end_date}")
        
        try:
            # Fetch all data types
            afrr_prices = self.fetch_afrr_capacity_prices(start_date, end_date)
            afrr_volumes = self.fetch_afrr_volumes(start_date, end_date)
            energy_prices = self.fetch_energy_prices(start_date, end_date)
            
            print(f"🎉 Successfully fetched all PSE data:")
            print(f"   - aFRR capacity prices: {len(afrr_prices)} records")
            print(f"   - aFRR volumes: {len(afrr_volumes)} records") 
            print(f"   - Energy prices: {len(energy_prices)} records")
            
            return {
                "afrr_prices": afrr_prices,
                "afrr_volumes": afrr_volumes,
                "energy_prices": energy_prices,
                "success": True
            }
            
        except Exception as e:
            print(f"💥 PSE data fetch failed: {e}")
            return {"success": False, "error": str(e)}

if __name__ == "__main__":
    # Command line usage
    import sys
    
    fetcher = PSEDataFetcher()
    
    if len(sys.argv) >= 3:
        start_date = sys.argv[1]
        end_date = sys.argv[2]
    else:
        start_date = "2024-07-01"
        end_date = "2025-06-30"
    
    result = fetcher.fetch_all_data(start_date, end_date)
    
    if result["success"]:
        print("✅ PSE data fetch completed successfully")
        sys.exit(0)
    else:
        print("❌ PSE data fetch failed")
        sys.exit(1)