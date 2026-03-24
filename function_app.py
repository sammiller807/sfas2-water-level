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
import json
import azure.functions as func
import requests
import time
from noaa_coops import Station
from datetime import datetime, timedelta, timezone

app = func.FunctionApp()

@app.timer_trigger(schedule="0 */15 * * * *", arg_name="myTimer", run_on_startup=False,
              use_monitor=False)
@app.sql_output(arg_name="observations",
                command_text="observations",
                connection_string_setting="SqlConnectionString")
@app.sql_input(arg_name="last_datetime_row",
               command_text="SELECT ISNULL(MAX(datetime_utc), DATEADD(DAY, -7, GETUTCDATE())) AS last_dt FROM observations",
               connection_string_setting="SqlConnectionString")
def obsData(myTimer: func.TimerRequest, observations: func.Out[func.SqlRowList], last_datetime_row: func.SqlRowList) -> None:
    logging.info(f"Last datetime row from database: {last_datetime_row[0]['last_dt']}")
    
    logging.info('Python timer trigger function executed.')

    if myTimer.past_due:
        logging.warning('Timer trigger is running past due')

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
                        datum="NAVD",
                        units="metric",
                        time_zone="gmt",
                    )
                    
                    if df.empty:
                        logging.info(f"NOAA {product}: No data available")
                        continue
                    
                    df = df.reset_index()
                    df.rename(columns={"t": "datetime_utc", "v": "value"}, inplace=True)
                    
                    logging.info(f"NOAA {product}: {len(df)} records retrieved")
                    
                    # Map NOAA products to parameter names
                    param_name_map = {
                        "water_level": "tidal_elevation",
                        "water_temperature": "water_temperature",
                        "air_temperature": "air_temperature",
                    }

                    unit_map = {
                        "water_level": "meters",
                        "water_temperature": "celsius",
                        "air_temperature": "celsius",
                    }
                    
                    # Transform each row to database schema format
                    for _, row in df.iterrows():
                        if row["value"] is None:
                            continue

                        # Ensure datetime is a plain ISO string (pandas.Timestamp isn't JSON serializable)
                        dt_val = row["datetime_utc"]
                        if hasattr(dt_val, "isoformat"):
                            dt_val = dt_val.isoformat()

                        observations.append({
                            "station_id": NOAA_STATION,
                            "parameter": param_name_map[product],
                            "value": float(row["value"]),
                            "units": unit_map[product],
                            "datetime_utc": dt_val
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

        # Normalize and convert USGS values into the units we want (meters / celsius)
        def _normalize_usgs_value(param_code: str, unit_code: str, value: float) -> tuple[float, str]:
            """Convert requested USGS units into a consistent metric output."""
            # Common USGS unit codes we might see
            if param_code == "72279":
                # Tidal elevation: prefer meters
                if unit_code in ("ft", "feet", "foot"):
                    return value * 0.3048, "meters"
                if unit_code in ("m", "meters"):
                    return value, "meters"
                return value, "meters"

            if param_code in ("00010", "00020"):  # temperature
                return value, "celsius"

            # Fallback: return the original unit code if we don't know it
            return value, unit_code

        # Format dates for API (UTC without redundant offset)
        # USGS doesn't accept the "+00:00Z" suffix produced by isoformat(),
        # so we manually construct an ISO string with a trailing Z.
        start_str = start_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        end_str = end_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        logging.info(f"Fetching USGS data from {start_str} to {end_str}...")

        # Fetch each site individually to avoid errors
        for site_id in USGS_SITES:
            # wrap request in a simple retry loop to cope with transient network/HTTP issues
            url = "https://nwis.waterservices.usgs.gov/nwis/iv"
            params = {
                "format": "json",
                "sites": site_id,
                "parameterCd": ",".join(PARAM_MAP.keys()),
                "startDT": start_str,
                "endDT": end_str,
            }

            attempt = 0
            data = None
            while attempt < 3:
                try:
                    response = requests.get(url, params=params, timeout=30)
                    response.raise_for_status()
                    data = response.json()
                    break
                except requests.exceptions.RequestException as e:
                    attempt += 1
                    logging.warning(f"Site {site_id}: request attempt {attempt} failed - {e}")
                    if attempt >= 3:
                        # give up on this site, move to next
                        data = None
                        break
                    # simple exponential backoff
                    time.sleep(2 ** attempt)

            # if we didn't get any data after retries, skip this site
            if data is None:
                continue

            # parse the returned JSON, handling possible missing fields
            try:
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

                        val, units = _normalize_usgs_value(param_code, unit_code, float(v["value"]))

                        observations.append({
                            "station_id": site_id,
                            "parameter": param,
                            "value": val,
                            "units": units,
                            "datetime_utc": v["dateTime"]
                        })

                        site_records += 1

                logging.info(f"Site {site_id}: {site_records} records retrieved")

            except (KeyError, ValueError, IndexError) as e:
                logging.warning(f"Site {site_id}: Parse error - {e}")
            except requests.exceptions.RequestException as e:
                # although we handled network errors in the retry loop, keep this
                logging.warning(f"Site {site_id}: Connection error - {e}")

        logging.info(f"USGS observations: {len(observations)} records")
        return observations
    
    end_dt = datetime.now(timezone.utc)

    def _ensure_utc(dt: datetime) -> datetime:
        """Ensure a datetime has UTC tzinfo so .astimezone() behaves predictably."""
        if dt is None:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    # Extract the latest datetime from the input binding, then advance slightly to avoid overlapping
    if last_datetime_row:
        last_dt_str = last_datetime_row[0]["last_dt"]
        # Parse the datetime string (Azure SQL returns it as a string)
        last_dt = datetime.fromisoformat(last_dt_str.replace('Z', '+00:00'))  # Handle UTC format
        last_dt = _ensure_utc(last_dt)
        # Increment a small amount so we don't refetch the same timestamp (avoids unique key conflicts)
        start_dt = last_dt + timedelta(minutes=1)
    else:
        start_dt = end_dt - timedelta(days=7)  # Fallback

    # Ensure start_dt is not in the future (edge case) and not after end_dt
    if start_dt >= end_dt:
        start_dt = end_dt - timedelta(minutes=15)
    
    logging.info(f"Fetching data from {start_dt} to {end_dt}...")

    try:
        logging.info("Fetching NOAA data...")
        noaa_obs = fetch_noaa(start_dt, end_dt)
        
        logging.info("Fetching USGS data...")
        usgs_obs = fetch_usgs(start_dt, end_dt)
        
        # Combine observations from both sources
        all_observations = noaa_obs + usgs_obs
        logging.info(f"Total observations from both sources: {len(all_observations)} records")
        
        # Convert observations to SqlRow objects and insert via output binding
        if all_observations:
            logging.info(f"Inserting {len(all_observations)} observations into database...")
            rows = func.SqlRowList()
            for obs in all_observations:
                row = func.SqlRow.from_dict(obs)
                rows.append(row)
            observations.set(rows)
            logging.info("Observations inserted successfully")
        
    except Exception as e:
        logging.error(f"Error in timer trigger: {e}", exc_info=True)


@app.route(route="stations", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
@app.sql_input(arg_name="sql_rows",
               command_text="SELECT DISTINCT station_id FROM observations ORDER BY station_id",
               connection_string_setting="SqlConnectionString")
def getStations(req: func.HttpRequest, sql_rows: func.SqlRowList) -> func.HttpResponse:
    """HTTP trigger function to get list of unique stations from the database."""
    logging.info('getStations HTTP trigger function called.')
    
    try:
        stations = []
        for row in sql_rows:
            stations.append(row["station_id"])
        
        return func.HttpResponse(
            json.dumps(stations),
            status_code=200,
            mimetype="application/json"
        )
    except Exception as e:
        logging.error(f"Error retrieving stations: {e}")
        return func.HttpResponse(
            json.dumps({"error": "Failed to retrieve stations"}),
            status_code=500,
            mimetype="application/json"
        )


@app.route(route="station-data/{station_id}/{start_date}/{end_date}", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
@app.sql_input(
    arg_name="station_rows",
    command_text="SELECT station_id, parameter, value, units, datetime_utc FROM observations WHERE station_id = @station_id AND datetime_utc BETWEEN @start_date AND @end_date ORDER BY datetime_utc",
    connection_string_setting="SqlConnectionString",
    parameters="@station_id={station_id},@start_date={start_date},@end_date={end_date}"
)
def getStationData(
    req: func.HttpRequest,
    station_rows: func.SqlRowList
) -> func.HttpResponse:
    """HTTP trigger function to fetch observations for a given station and date range."""
    station_id = req.route_params.get("station_id")
    start_date = req.route_params.get("start_date")
    end_date = req.route_params.get("end_date")
    logging.info('getStationData HTTP trigger function called. station_id=%s start_date=%s end_date=%s', station_id, start_date, end_date)

    try:
        results = []
        for row in station_rows:
            results.append({
                "station_id": row["station_id"],
                "parameter": row["parameter"],
                "value": row["value"],
                "units": row.get("units"),
                "datetime_utc": row["datetime_utc"],
            })

        return func.HttpResponse(
            json.dumps(results),
            status_code=200,
            mimetype="application/json"
        )
    except Exception as e:
        logging.error(f"Error retrieving station data: {e}")
        return func.HttpResponse(
            json.dumps({"error": "Failed to retrieve station data"}),
            status_code=500,
            mimetype="application/json"
        )


@app.route(route="station-details", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def getStationDetails(req: func.HttpRequest) -> func.HttpResponse:
    """HTTP trigger function to proxy the external JS file with station data."""
    logging.info('getStationDetails HTTP trigger function called.')
    
    try:
        # Fetch the external JS file
        url = "http://hudson.dl.stevens-tech.edu/sfas/sfas_stations2.js"
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        
        # Return the JS content with appropriate headers
        return func.HttpResponse(
            response.text,
            status_code=200,
            mimetype="application/javascript"
        )
    except requests.exceptions.RequestException as e:
        logging.error(f"Error fetching station details: {e}")
        return func.HttpResponse(
            "/* Error fetching station data */",
            status_code=500,
            mimetype="application/javascript"
        )
    except Exception as e:
        logging.error(f"Unexpected error in getStationDetails: {e}")
        return func.HttpResponse(
            "/* Internal server error */",
            status_code=500,
            mimetype="application/javascript"
        )