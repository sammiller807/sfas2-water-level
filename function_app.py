'''
Azure SQL Schema:
    CREATE TABLE observations (
    id INT IDENTITY PRIMARY KEY,
    station_id VARCHAR(10) NOT NULL,
    parameter VARCHAR(20) NOT NULL,
    value FLOAT NOT NULL,
    units VARCHAR(10) NULL,
    datetime_utc DATETIME2 NOT NULL
    );
'''

import logging
import azure.functions as func
import requests
from noaa_coops import Station
import pandas as pd
from datetime import datetime, timedelta, timezone
import json

app = func.FunctionApp()

@app.timer_trigger(schedule="0 */15 * * * *", arg_name="myTimer", run_on_startup=False,
              use_monitor=False)

def obsData(myTimer: func.TimerRequest) -> None:
    logging.info('Python timer trigger function executed.')

    NOAA_STATION = "8534720"  # Atlantic City NJ

    # USGS Station IDs
    USGS_SITES = [
        "01408048",  # Watson Creek at Manasquan NJ
        "01408168",  # Barnegat Bay at Mantoloking NJ
        "01408750",  # Barnegat Bay at Seaside Heights NJ
        "01409110",  # Barnegat Bay at Waretown NJ
        "01409125",  # Barnegat Bay at Barnegat Light NJ
        "01409146",  # East Thorofare at Ship Bottom NJ
        "01409335",  # Little Egg Inlet near Tuckerton NJ
        "01410510",  # Absecon Creek Rte 30 at Absecon NJ
        "01410560"   # Inside Thorofare Rte 40 Atlantic City NJ
    ]

    # USGS Parameter Codes:
    # "72279": "Tidal elevation, NOS-averaged, NAVD88, feet",
    # "00010": "Temperature, water, degrees Celsius",
    # "00020": "Temperature, air, degrees Celsius",
    # "00065": "Gage height, feet"
    PARAM_MAP = {
        "72279": "tidal_elevation",
        "00010": "water_temperature",
    }

    def fetch_noaa(start_dt: datetime, end_dt: datetime) -> list:
        """Fetch NOAA data from NOAA COOPS API for all products.
        
        Args:
            start_dt: Start datetime
            end_dt: End datetime
            
        Returns:
            List of dicts with schema: {station_id, parameter, value, units, datetime_utc}
        """
        observations = []
        
        try:
            station = Station(id=NOAA_STATION)
            
            # Format dates for NOAA API
            start_str = start_dt.strftime("%Y%m%d %H:%M")
            end_str = end_dt.strftime("%Y%m%d %H:%M")
            
            # Fetch each product and consolidate
            for product in ["water_level", "water_temperature", "air_temperature"]:
                try:
                    df = station.get_data(
                        begin_date=start_str,
                        end_date=end_str,
                        product=product,
                    )
                    
                    if df.empty:
                        logging.info(f"NOAA {product}: No data available")
                        continue
                    
                    df = df.reset_index()
                    df.rename(columns={"t": "datetime_utc", "v": "value"}, inplace=True)
                    
                    logging.info(f"NOAA {product}: {len(df)} records retrieved")
                    
                    # Map NOAA products to parameter names
                    param_name_map = {
                        "water_level": "water_elevation",
                        "water_temperature": "water_temperature",
                        "air_temperature": "air_temperature",
                    }
                    
                    # Transform each row to database schema format
                    for _, row in df.iterrows():
                        if row["value"] is None:
                            continue
                            
                        observations.append({
                            "station_id": NOAA_STATION,
                            "parameter": param_name_map[product],
                            "value": float(row["value"]),
                            "units": "metric",
                            "datetime_utc": row["datetime_utc"]
                        })
                            
                except Exception as e:
                    logging.warning(f"Error fetching NOAA {product}: {e}")
            
            logging.info(f"NOAA observations: {len(observations)} records")
            
        except Exception as e:
            logging.error(f"Error fetching NOAA data: {e}")
        
        return observations
    
    def fetch_usgs(start_dt: datetime, end_dt: datetime) -> list:
        """Fetch USGS data using Instantaneous Values API (NWIS IV).
        
        Args:
            start_dt: Start datetime
            end_dt: End datetime
            
        Returns:
            List of dicts with schema: {station_id, parameter, value, units, datetime_utc}
        """
        observations = []
        
        # Format dates for API (ISO format)
        start_str = start_dt.isoformat() + "Z"
        end_str = end_dt.isoformat() + "Z"
        
        logging.info(f"Fetching USGS data from {start_str} to {end_str}...")
        
        # Fetch each site individually to avoid errors
        for site_id in USGS_SITES:
            try:
                url = "https://nwis.waterservices.usgs.gov/nwis/iv"
                params = {
                    "format": "json",
                    "sites": site_id,
                    "parameterCd": ",".join(PARAM_MAP.keys()),
                    "startDT": start_str,
                    "endDT": end_str
                }
                
                response = requests.get(url, params=params, timeout=30)
                response.raise_for_status()
                
                data = response.json()
                
                if "value" not in data or "timeSeries" not in data.get("value", {}):
                    logging.debug(f"Site {site_id}: No data available")
                    continue
                
                time_series_list = data["value"]["timeSeries"]
                if not time_series_list:
                    logging.debug(f"Site {site_id}: No observations in date range")
                    continue
                
                site_records = 0
                
                # Parse each time series (one per parameter) and transform to schema format
                for ts in time_series_list:
                    param_code = ts["variable"]["variableCode"][0]["value"]
                    
                    if param_code not in PARAM_MAP:
                        continue
                    
                    param = PARAM_MAP[param_code]
                    unit_code = ts["variable"]["unit"]["unitCode"]
                    
                    # Handle case where values might be in different structure
                    values_list = ts.get("values", [{}])[0].get("value", [])
                    
                    for v in values_list:
                        if v.get("value") is None:
                            continue
                        
                        observations.append({
                            "station_id": site_id,
                            "parameter": param,
                            "value": float(v["value"]),
                            "units": unit_code,
                            "datetime_utc": v["dateTime"]
                        })
                        
                        site_records += 1
                
                logging.info(f"Site {site_id}: {site_records} records retrieved")
                
            except requests.exceptions.RequestException as e:
                logging.warning(f"Site {site_id}: Connection error - {e}")
            except (KeyError, ValueError, IndexError) as e:
                logging.warning(f"Site {site_id}: Parse error - {e}")
        
        logging.info(f"USGS observations: {len(observations)} records")
        return observations
    
    end_dt = datetime.now(timezone.utc)

    # This will be assigned based on the last datetime in the database, which will be grabbed with an SQL Input Binding
    # Default to past 7 days
    start_dt = end_dt - timedelta(days=7)
    
    try:
        logging.info("Fetching NOAA data...")
        noaa_obs = fetch_noaa(start_dt, end_dt)
        
        logging.info("Fetching USGS data...")
        usgs_obs = fetch_usgs(start_dt, end_dt)
        
        # Combine observations from both sources
        all_observations = noaa_obs + usgs_obs
        logging.info(f"Total observations from both sources: {len(all_observations)} records")
        
        # Log observations for database insertion (can be used with Azure bindings)
        if all_observations:
            logging.info(f"Ready to insert: {json.dumps(all_observations[:3])}")  # Log first 3 as sample
        
    except Exception as e:
        logging.error(f"Error in timer trigger: {e}", exc_info=True)