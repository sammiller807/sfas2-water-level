# Water Level Data Collection Script

This Python script collects continuous tidal elevation and water temperature data from NOAA and USGS monitoring stations in New Jersey, storing the time series data in an Azure SQL database.

## Features

- **NOAA Data**: Fetches water level, water temperature, and air temperature from Atlantic City station (8534720)
- **USGS Data**: Fetches water level and water temperature from 9 continuous monitoring sites across New Jersey using the new Instantaneous Values API
- **Dynamic Time Range**: Automatically fetches data from the last successful script run to the current time
- **Database Integration**: Stores observations in Azure SQL with duplicate detection
- **Runtime Tracking**: Logs execution details and runtime information
- **Comprehensive Logging**: Detailed logging of all operations and errors

## Data Sources

### NOAA Station
- **Atlantic City** (Station ID: 8534720)

### USGS Sites (New Jersey)
1. Watson Creek at Manasquan (01408048)
2. Barnegat Bay at Mantoloking (01408168)
3. Barnegat Bay at Seaside Heights (01408750)
4. Barnegat Bay at Waretown (01409110)
5. Barnegat Bay at Barnegat Light (01409125)
6. East Thorofare at Ship Bottom (01409146)
7. Little Egg Inlet near Tuckerton (01409335)
8. Absecon Creek Rte 30 at Absecon (01410510)
9. Inside Thorofare Rte 40 Atlantic City (01410560)

## Installation

### Prerequisites
- Python 3.8+
- Azure SQL Database
- ODBC Driver 17 for SQL Server (for pyodbc connection on Linux/Mac)

### Setup Steps

1. **Clone the repository**
   ```bash
   git clone <repository_url>
   cd sfas2-water-level
   ```

2. **Create a virtual environment**
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

4. **Set up environment variables**
   ```bash
   cp .env.example .env
   ```
   
   Edit `.env` and add:
   - `API_KEY`: Optional USGS API key
   - `DB_CONNECTION_STRING`: Your Azure SQL database connection string

5. **Create database tables**
   Run the SQL schema from the script header in your Azure SQL database. See [SCHEMA_CHANGES.md](SCHEMA_CHANGES.md) for detailed schema documentation and design rationale.
   
   Copy the SQL from the script header comments, or:
   ```sql
   CREATE TABLE observations (
       id BIGINT IDENTITY PRIMARY KEY,
       station_id VARCHAR(20) NOT NULL,
       datetime_utc DATETIME2 NOT NULL,
       water_elevation NUMERIC(10,4) NULL,
       water_elevation_units VARCHAR(20),
       water_elevation_quality VARCHAR(20),
       water_temperature NUMERIC(10,4) NULL,
       water_temperature_units VARCHAR(20),
       water_temperature_quality VARCHAR(20),
       air_temperature NUMERIC(10,4) NULL,
       air_temperature_units VARCHAR(20),
       air_temperature_quality VARCHAR(20),
       inserted_at DATETIME2 DEFAULT SYSUTCDATETIME(),
       CONSTRAINT uq_obs UNIQUE (station_id, datetime_utc),
       CONSTRAINT chk_water_elev CHECK (water_elevation IS NULL OR (water_elevation >= -10 AND water_elevation <= 10)),
       CONSTRAINT chk_water_temp CHECK (water_temperature IS NULL OR (water_temperature >= -5 AND water_temperature <= 50)),
       CONSTRAINT chk_air_temp CHECK (air_temperature IS NULL OR (air_temperature >= -20 AND air_temperature <= 50))
   );

   CREATE TABLE script_runtime (
       id BIGINT IDENTITY PRIMARY KEY,
       start_time DATETIME2 NOT NULL,
       end_time DATETIME2 NOT NULL,
       records_inserted INT,
       records_duplicated INT DEFAULT 0,
       status VARCHAR(50),
       error_message VARCHAR(500),
       created_at DATETIME2 DEFAULT SYSUTCDATETIME()
   );

   CREATE INDEX idx_obs_station_datetime ON observations(station_id, datetime_utc DESC);
   CREATE INDEX idx_obs_datetime ON observations(datetime_utc DESC);
   ```

## Usage

### Run the script
```bash
python usgs_ny_water_data.py
```

### Scheduled Execution
Set up a cron job (Linux/Mac) or Task Scheduler (Windows) to run the script periodically:

**Cron example** (every hour):
```bash
0 * * * * cd /path/to/sfas2-water-level && python usgs_ny_water_data.py
```

## Configuration

### Environment Variables

| Variable | Description | Required |
|----------|-------------|----------|
| `API_KEY` | USGS WaterData API key | Optional |
| `DB_CONNECTION_STRING` | Azure SQL connection string | Yes |

### Connection String Format
```
Driver={ODBC Driver 17 for SQL Server};Server=<server_name>.database.windows.net;Database=<database_name>;UID=<username>;PWD=<password>;
```

## API Details

### NOAA COOPS API
- Endpoint: NOAA Cooperative Ocean Observation System
- Products: water_level, water_temperature, air_temperature
- Datum: MLLW (Mean Lower Low Water)
- Units: Metric
- Timezone: GMT

### USGS Instantaneous Values API
- **Old Endpoint** (being decommissioned): `https://nwis.waterservices.usgs.gov/nwis/iv/`
- **New Endpoint** (used by this script): `https://nwis.waterservices.usgs.gov/api/nwis/iv`
- Parameters:
  - `62615`: Gage height (water level) in feet
  - `00010`: Temperature, water in °C

## Database Schema

### observations Table

Stores all water observation records with one row per station per timestamp. Uses a **column-per-metric** design for optimal time-series queries:

- `station_id`: Station identifier (e.g., "8534720" for NOAA, "01408048" for USGS)
- `datetime_utc`: UTC timestamp of observation
- `water_elevation`: Continuous tidal elevation (meters, -10 to 10)
- `water_elevation_units`: Units for water elevation (typically "metric")
- `water_elevation_quality`: Quality flag for water elevation measurement
- `water_temperature`: Water temperature (°C, -5 to 50)
- `water_temperature_units`: Units for water temperature (typically "°C")
- `water_temperature_quality`: Quality flag for water temperature
- `air_temperature`: Air temperature (°C, -20 to 50)
- `air_temperature_units`: Units for air temperature (typically "°C")
- `air_temperature_quality`: Quality flag for air temperature
- `inserted_at`: Record insertion timestamp

Unique constraint on `(station_id, datetime_utc)` prevents duplicate inserts. CHECK constraints validate data ranges.

**Key Features:**
- One row represents all available measurements at a station/time
- NULL values indicate missing/unavailable measurements
- Each parameter has independent units and quality tracking
- 66% more efficient than row-per-parameter design

See [SCHEMA_CHANGES.md](SCHEMA_CHANGES.md) for detailed design rationale and migration information.

### script_runtime Table

Logs execution details:
- `start_time`: Script execution start time
- `end_time`: Script execution end time
- `records_inserted`: Count of successfully inserted records
- `records_duplicated`: Count of duplicate records skipped
- `status`: Execution status ('success', 'partial', 'error')
- `error_message`: Optional error details for debugging
- `created_at`: Timestamp when this log entry was created

## Troubleshooting

### Database Connection Issues
- Verify Azure SQL server is accessible from your network
- Check firewall rules allow your IP
- Ensure ODBC driver is installed: `sudo apt install odbcinst` (Linux)

### API Connection Errors
- Check internet connectivity
- Verify API endpoints are accessible
- Review API rate limits and quotas

### No Data Retrieved
- Verify date range is correct
- Check station IDs are valid and have recent data
- Review NOAA/USGS API status pages

## Logging

The script generates detailed logs including:
- Data fetch operations and record counts
- Database connection and insertion details
- Error conditions and exceptions
- Script execution timeline

Logs are displayed in the console with timestamps and severity levels.
