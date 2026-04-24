import logging
import json
import azure.functions as func
import requests
from noaa_coops import Station
from datetime import datetime, timedelta, timezone
from dataretrieval import waterdata
from dotenv import load_dotenv

app = func.FunctionApp()
load_dotenv()

@app.timer_trigger(schedule="0 */15 * * * *", arg_name="myTimer", run_on_startup=False, use_monitor=False)
@app.sql_input(arg_name="station_list", command_text="SELECT sid, agency, agency_sid FROM ext_stations", connection_string_setting="SqlConnectionString")
@app.sql_input(arg_name="latest_data_list", command_text="SELECT sid, param, dt FROM ext_latest_data", connection_string_setting="SqlConnectionString")
@app.sql_output(arg_name="output_list", command_text="ext_observations", connection_string_setting="SqlConnectionString")
@app.sql_output(arg_name="output_latest_data_list", command_text="ext_latest_data", connection_string_setting="SqlConnectionString")
def fetchObservationData(myTimer: func.TimerRequest, station_list: func.SqlRowList, latest_data_list: func.SqlRowList, output_list: func.Out[func.SqlRowList], output_latest_data_list: func.Out[func.SqlRowList]) -> None:
    logging.info('Timer trigger function executed.')

    if myTimer.past_due:
        logging.warning('Timer trigger is running past due')

    # USGS Parameter Codes:
    # "72279": "Tidal elevation, NOS-averaged, NAVD88, feet",
    # "00010": "Temperature, water, degrees Celsius",
    # "00020": "Temperature, air, degrees Celsius",
    # "00065": "Gage height, feet"
    USGS_PARAM_CODES = {
        "PWL": "72279"
    }

    # NOAA Products:
    # water_level: Preliminary or verified 6-minute interval water levels, depending on data availability.
    # air_temperature: Air temperature as measured at the station.
    # water_temperature: Water temperature as measured at the station.
    NOS_PRODUCTS = {
        "PWL": "water_level"
    }

    #NOTE: PWL is Preliminary/Provisonal Water Level

    #NOTE: For one transaction, all observation data will be put into this one array
    all_observations = []

    #NOTE: Store the updated parameters/products in this list -> will be used to update the database
    update_latest_data = []

    # DEFAULT START AND END DATES
    END_DATE = datetime.now(timezone.utc)
    START_DATE = END_DATE - timedelta(days=7)

    def fetch_noaa(station: dict[str, str]) -> None:
        """Fetch NOAA data from NOAA COOPS API for all products.

        https://api.tidesandcurrents.noaa.gov/api/prod/
        
        Args:
            station: dictionary of station data. Expects keys: sid, agency, agency_sid
            end_dt: The current datetime. Should be UTC
        """

        station_obj = Station(station["agency_sid"])

        nos_observations = []
        
        # Format dates for NOAA API
        # All dates can be formatted as follows:
        # yyyyMMdd, yyyyMMdd HH:mm, MM/dd/yyyy, or MM/dd/yyyy HH:mm

        # Python datetime info can be found here: https://docs.python.org/3/library/datetime.html
        # "%Y%m%d %H:%M" == yyyyMMdd HH:mm
        end_str = END_DATE.strftime("%Y%m%d %H:%M")
        start_str = START_DATE.strftime("%Y%m%d %H:%M")

        # Lookup dict for latest_data
        lookup = {}

        # Checks if there is data stored in the ext_latest_data table. If there is, create a lookup dict based on the parameter, and then change the start string based on the date.
        if latest_data_list:
            nos_latest_data = []

            for latest_data in latest_data_list:
                if(station["sid"] == latest_data["sid"]):
                    nos_latest_data.append(latest_data)
        
            lookup = {latest_data["param"]: latest_data for latest_data in nos_latest_data}

        for param, product in NOS_PRODUCTS.items():
            if lookup and param in lookup:
                #NOTE: If product is in lookup, this will change start str to the last saved date
                start_str = datetime.fromisoformat(lookup[param]["dt"]).strftime("%Y%m%d %H:%M")
            else:
                start_str = START_DATE.strftime("%Y%m%d %H:%M")
            
            try:
                if(product == "water_level"):
                    df = station_obj.get_data(
                    begin_date=start_str,
                    end_date=end_str,
                    product=product,
                    datum="NAVD",
                    units="metric",
                    time_zone="gmt",
                )
                else:
                    df = station_obj.get_data(
                    begin_date=start_str,
                    end_date=end_str,
                    product=product,
                    units="metric",
                    time_zone="gmt",
                )

                if df.empty:
                    logging.info(f"NOAA station '{station["sid"]}' {param}: No data available")
                    continue

                logging.info(f"NOAA station {station["sid"]} {param}: {len(df)} records retrieved")

                # Reset the index (because the time column is the index for whatever reason)
                df.reset_index(inplace=True)

                # Convert timestamp to datetime string
                df["t"] = df["t"].apply(lambda x: x.isoformat())


                #Iterate over the dataframe and append them to the observations
                # NOAA API Response Help: https://api.tidesandcurrents.noaa.gov/api/prod/responseHelp.html
                for row in df.itertuples():
                    nos_observations.append({
                        "sid": station["sid"],
                        "param": param,
                        "dt": row.t,
                        "val": row.v,
                        })
                
                #For the last row, set the ext_latest_data for the param
                last_row = df.iloc[-1]
                update_latest_data.append({
                    "sid": station["sid"],
                    "param": param,
                    "dt": last_row.t,
                    "val": last_row.v,
                    "dt_last_upd": END_DATE.isoformat()
                })
                
            except Exception as e:
                logging.warning(f"Error fetching NOAA station {station["sid"]} {param}: {e}")
            
        logging.info(f"NOAA observations: {len(nos_observations)} records")
        all_observations.extend(nos_observations)
    
    def fetch_usgs(station: dict[str, str]) -> None:
        """Fetch USGS data using Instantaneous Values API (NWIS IV).
        
        Args:
            station: dictionary of station data. Expects keys: sid, agency, agency_sid
        """

        usgs_observations = []

        end_str = END_DATE.isoformat()
        start_str = START_DATE.isoformat()

        # Lookup dict for latest_data
        lookup = {}

        # Checks if there is data stored in the ext_latest_data table. If there is, create a lookup dict based on the parameter, and then change the start string based on the date.
        if latest_data_list:
            usgs_latest_data = []

            for latest_data in latest_data_list:
                if(station["sid"] == latest_data["sid"]):
                    usgs_latest_data.append(latest_data)
        
            lookup = {latest_data["param"]: latest_data for latest_data in usgs_latest_data}

        for param, code in USGS_PARAM_CODES.items():
            if lookup and param in lookup:
                #NOTE: If product is in look, this will change start str to the last saved date
                start_str = datetime.fromisoformat(lookup[param]["dt"]).isoformat()
            else:
                start_str = START_DATE.isoformat()

            try:
                df, metadata = waterdata.get_continuous(
                    monitoring_location_id=f"USGS-{station['agency_sid']}",
                    parameter_code=code,
                    time=f"{start_str}/{end_str}"
                )

                # Convert value column into meters
                df["value"] = df["value"] * 0.3048

                # Convert timestamp to datetime string
                df["time"] = df["time"].apply(lambda x: x.isoformat())

                for row in df.itertuples():
                    usgs_observations.append({
                        "sid": station["sid"],
                        "param": param,
                        "dt": row.time,
                        "val": row.value,
                        })
                    
                #For the last row, set the ext_latest_data for the param
                last_row = df.iloc[-1]
                update_latest_data.append({
                    "sid": station["sid"],
                    "param": param,
                    "dt": last_row.time,
                    "val": last_row.value,
                    "dt_last_upd": END_DATE.isoformat()
                })
            except Exception as e:
                logging.warning(f"Error fetching USGS station {station["sid"]} {param}: {e}")

        logging.info(f"USGS observations: {len(usgs_observations)} records")
        all_observations.extend(usgs_observations)
    
    logging.info(f"Fetching data at {END_DATE}...")

    try:
        #NOTE: station_list: [{"sid": "U222" or "N022","agency": "USGS" or "NOS","agency_sid": "1408168"}, ...]
        for station in station_list:
            if station.get("agency") == "NOS":
                fetch_noaa(station)
            elif station.get("agency") == "USGS":
                fetch_usgs(station)
            else:
                #NOTE: Call new functions for fetching data HERE
                continue
        
        logging.info(f"Total observations: {len(all_observations)} records")
        
        if all_observations:
            logging.info(f"Inserting {len(all_observations)} observations into database...")
            observation_rows = func.SqlRowList()
            for obs in all_observations:
                row = func.SqlRow.from_dict(obs)
                observation_rows.append(row)
            output_list.set(observation_rows)
            logging.info("Observations inserted successfully")

        if update_latest_data:
            logging.info(f"Inserting {len(update_latest_data)} latest_data into database...")
            latest_data_rows = func.SqlRowList()
            for data in update_latest_data:
                row = func.SqlRow.from_dict(data)
                latest_data_rows.append(row)
            output_latest_data_list.set(latest_data_rows)
            logging.info("Latest data inserted successfully")
        
    except Exception as e:
        logging.error(f"Error in timer trigger: {e}", exc_info=True)


@app.route(route="station-list", methods=["GET"], auth_level=func.AuthLevel.FUNCTION)
@app.sql_input(arg_name="station_list", command_text="SELECT sid, agency, agency_sid FROM ext_stations", connection_string_setting="SqlConnectionString")
def getStationList(req: func.HttpRequest, station_list: func.SqlRowList) -> func.HttpResponse:
    """HTTP trigger function to get list of unique stations from the database."""
    logging.info('getStations HTTP trigger function called.')
    
    try:
        stations = []
        for row in station_list:
            stations.append({
                "sid": row["sid"],
                "agency": row["agency"],
                "agency_sid": row["agency_sid"]
            })

        return func.HttpResponse(
            json.dumps(stations),
            status_code=200,
            mimetype="application/json"
        )
    except Exception as e:
        logging.error(f"Error retrieving station list: {e}")

        return func.HttpResponse(
            json.dumps({"error": "Failed to retrieve station list"}),
            status_code=500,
            mimetype="application/json"
        )


@app.route(route="station-data/{station_id}/{start_date}/{end_date}", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
@app.sql_input(
    arg_name="station_rows",
    command_text="SELECT sid, param, dt, val FROM ext_observations WHERE sid = @station_id AND TRY_CONVERT(datetime2, @start_date) IS NOT NULL AND TRY_CONVERT(datetime2, @end_date) IS NOT NULL AND dt >= TRY_CONVERT(datetime2, @start_date) AND dt <= TRY_CONVERT(datetime2, @end_date) ORDER BY dt;",
    connection_string_setting="SqlConnectionString",
    parameters="@station_id={station_id},@start_date={start_date},@end_date={end_date}"
)
def getStationDataTimeSeries(
    req: func.HttpRequest,
    station_rows: func.SqlRowList
) -> func.HttpResponse:
    """HTTP trigger function to fetch observations for a given station and date range."""
    station_id = req.route_params.get("station_id")
    start_date = req.route_params.get("start_date")
    end_date = req.route_params.get("end_date")
    logging.info('getStationData HTTP trigger function called: station_id=%s start_date=%s end_date=%s', station_id, start_date, end_date)

    try:
        stations = []
        for row in station_rows:
            stations.append({
                "sid": row["sid"],
                "param": row["param"],
                "dt": row["dt"],
                "val": row["val"]
            })

        return func.HttpResponse(
            json.dumps(stations),
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
def getHudsonStationDetails(req: func.HttpRequest) -> func.HttpResponse:
    """HTTP trigger function to get the Hudson server JS file."""
    logging.info('getStationDetails HTTP trigger function called.')
    
    try:
        response = requests.get("http://hudson.dl.stevens-tech.edu/sfas/sfas_stations2.js", timeout=30)
        response.raise_for_status()
        
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