import requests
from noaa_coops import Station
import pandas as pd
import os
from datetime import datetime, timedelta
from typing import Optional, Tuple
import logging

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

API_KEY = os.environ.get("API_KEY")
DB_CONNECTION_STRING = os.environ.get("DB_CONNECTION_STRING")

NOAA_STATION = "8534720"  # Atlantic City

# NOTE: Ignore gage height and air temp, the seaside heights and waretown locations don't have new data

# USGS Station IDs for continuous water level monitoring in NJ
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
    "00020": "air_temperature",
    "00065": "gage_height"
}

def fetch_noaa(start_dt: datetime, end_dt: datetime) -> list:
    """Fetch NOAA data from NOAA COOPS API for all products.
    
    Returns observations in narrow format (one row per parameter per datetime).
    
    Args:
        start_dt: Start datetime
        end_dt: End datetime
        
    Returns:
        List of observations in narrow format
    """
    observations = []
    
    try:
        st = Station(id=NOAA_STATION)
        
        # Format dates for NOAA API
        start_str = start_dt.strftime("%Y%m%d %H:%M")
        end_str = end_dt.strftime("%Y%m%d %H:%M")
        
        # Fetch each product and create narrow format rows
        for product in ["water_level", "water_temperature", "air_temperature"]:
            try:
                df = st.get_data(
                    begin_date=start_str,
                    end_date=end_str,
                    product=product
                )
                
                if df.empty:
                    logger.info(f"NOAA {product}: No data available")
                    continue
                
                df = df.reset_index()
                df.rename(columns={"t": "datetime_utc", "v": "value"}, inplace=True)
                
                logger.info(f"NOAA {product}: {len(df)} records retrieved")
                
                if product == "water_level":
                    product = "water_elevation"
                
                # Create narrow format rows
                for _, row in df.iterrows():
                    if row["value"]:
                        observations.append({
                            "station_id": NOAA_STATION,
                            "parameter": product,
                            "value": float(row["value"]),
                            "units": "metric",
                            "datetime_utc": row["datetime_utc"]
                        })
                        
            except Exception as e:
                logger.warning(f"Error fetching NOAA {product}: {e}")
        
        logger.info(f"NOAA observations: {len(observations)} records")
        
    except Exception as e:
        logger.error(f"Error fetching NOAA data: {e}")
    
    return observations

def fetch_usgs(start_dt: datetime, end_dt: datetime) -> list:
    """Fetch USGS data using Instantaneous Values API (NWIS IV).
    
    Returns observations in narrow format (one row per parameter per datetime).
    
    Args:
        start_dt: Start datetime
        end_dt: End datetime
        
    Returns:
        List of observations in narrow format
    """
    observations = []
    
    # Format dates for API (ISO format)
    start_str = start_dt.isoformat() + "Z"
    end_str = end_dt.isoformat() + "Z"
    
    logger.info(f"Fetching USGS data from {start_str} to {end_str}...")
    
    headers = {"User-Agent": "WaterLevelDataCollector/1.0"}
    
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
            
            response = requests.get(url, params=params, headers=headers, timeout=30)
            response.raise_for_status()
            
            data = response.json()
            
            if "value" not in data or "timeSeries" not in data.get("value", {}):
                logger.debug(f"Site {site_id}: No data available")
                continue
            
            time_series_list = data["value"]["timeSeries"]
            if not time_series_list:
                logger.debug(f"Site {site_id}: No observations in date range")
                continue
            
            site_records = 0
            
            # Parse each time series (one per parameter) and create narrow format rows
            for ts in time_series_list:
                param_code = ts["variable"]["variableCode"][0]["value"]
                
                if param_code not in PARAM_MAP:
                    continue
                
                param = PARAM_MAP[param_code]
                unit_code = ts["variable"]["unit"]["unitCode"]
                
                # Map USGS parameter codes to database parameter names
                if param == "tidal_elevation":
                    param = "water_elevation"
                
                # Handle case where values might be in different structure
                values_list = ts.get("values", [{}])[0].get("value", [])
                
                for v in values_list:
                    if v.get("value") is None:
                        continue
                    
                    # Create narrow format row
                    observations.append({
                        "station_id": site_id,
                        "parameter": param,
                        "value": float(v["value"]),
                        "units": unit_code,
                        "datetime_utc": v["dateTime"]
                    })
                    site_records += 1
            
            logger.info(f"Site {site_id}: {site_records} records retrieved")
            
        except requests.exceptions.RequestException as e:
            logger.warning(f"Site {site_id}: Connection error - {e}")
        except (KeyError, ValueError, IndexError) as e:
            logger.warning(f"Site {site_id}: Parse error - {e}")
    
    logger.info(f"USGS observations: {len(observations)} records")
    return observations

def main():
    # Fetch NOAA data
        logger.info("\n" + "=" * 60)
        logger.info("FETCHING NOAA DATA")
        logger.info("=" * 60)
        
        noaa_obs = fetch_noaa(start_dt, end_dt)
        if noaa_obs:
            logger.info(f"NOAA: {len(noaa_obs)} records")
            # Log the actual data returned from NOAA (first 3 records as a table)
            sample_noaa = noaa_obs[:3]
            try:
                df_noaa = pd.DataFrame(sample_noaa)
                logger.info(f"NOAA sample data (table):\n{df_noaa.to_string(index=False)}")
            except Exception as e:
                logger.info(f"NOAA sample data: {sample_noaa} (table formatting failed: {e})")
            all_observations.extend(noaa_obs)
        else:
            logger.info("NOAA: No data available")
        
        # Fetch USGS data
        logger.info("\n" + "=" * 60)
        logger.info("FETCHING USGS DATA")
        logger.info("=" * 60)
        
        usgs_obs = fetch_usgs(start_dt, end_dt)
        if usgs_obs:
            logger.info(f"USGS: {len(usgs_obs)} records")
            # Log the actual data returned from USGS (first 3 records as a table)
            sample_usgs = usgs_obs[:3]
            try:
                df_usgs = pd.DataFrame(sample_usgs)
                logger.info(f"USGS sample data (table):\n{df_usgs.to_string(index=False)}")
            except Exception as e:
                logger.info(f"USGS sample data: {sample_usgs} (table formatting failed: {e})")
            all_observations.extend(usgs_obs)
        else:
            logger.info("USGS: No data retrieved")
        


if __name__ == "__main__":
    main()