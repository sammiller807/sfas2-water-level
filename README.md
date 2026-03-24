# sfas2-water-level Azure Function App

This repository contains an Azure Functions Python app that retrieves tidal elevation and water temperature data from NOAA and USGS monitoring stations and writes the measurements to an Azure SQL database. The project includes a timer-triggered ingestion function along with an HTTP endpoint for listing available stations.

---

## Features

- Scheduled ingestion every 15 minutes via a timer trigger
- NOAA COOPS data (water level, water/air temperature) from Atlantic City (8534720)
- USGS NWIS Instantaneous Values for nine New Jersey sites
- Azure SQL output binding to bulk insert observations
- HTTP GET `/stations` endpoint returns station IDs with station titles in JSON
- Robust error handling and detailed logging

---

## Setup & Installation

### Prerequisites

- Python 3.8 or later
- Azure Functions Core Tools (for local development)
- An Azure SQL Database with appropriate firewall rules
- Azure Functions Python worker (see Microsoft documentation)

### Getting started locally

```bash
git clone <repository_url>
cd sfas2-water-level

# create and activate virtual environment
python -m venv .venv
source .venv/bin/activate           # Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

Create or update `local.settings.json` with your connection string:

```json
{
  "IsEncrypted": false,
  "Values": {
    "AzureWebJobsStorage": "UseDevelopmentStorage=true",
    "FUNCTIONS_WORKER_RUNTIME": "python",
    "SqlConnectionString": "<your-Azure-SQL-connection-string>"
  }
}
```

> Note: The same `SqlConnectionString` setting is used by both the timer and HTTP functions.

If you already have the table created, run this migration:

```sql
ALTER TABLE observations ADD title VARCHAR(100) NULL;
```

### Database schema

Execute the following SQL in your Azure SQL instance (see `function_app.py` header comments for reference):

```sql
CREATE TABLE observations (
    id INT IDENTITY PRIMARY KEY,
    station_id VARCHAR(10) NOT NULL,
    title VARCHAR(100) NULL,
    parameter VARCHAR(20) NOT NULL,
    value FLOAT NOT NULL,
    units VARCHAR(10) NULL,
    datetime_utc DATETIME2 NOT NULL
);
```

Additional indexes and constraints can be added for performance or validation as needed.

---

## Running the app

Start the functions host locally:

```bash
func start
```

The timer function (`obsData`) executes automatically every 15 minutes. You can force a run by restarting the host.

### Testing the HTTP endpoint

```bash
curl http://localhost:7071/api/stations
```

Response example:

```json
[
  {"station_id": "01408048", "title": "Watson Creek at Manasquan NJ"},
  {"station_id": "01408168", "title": "Barnegat Bay at Mantoloking NJ"},
  {"station_id": "8534720", "title": "Atlantic City NJ"}
]
```

---

## Configuration

| Setting               | Description                                | Required |
|-----------------------|--------------------------------------------|----------|
| `SqlConnectionString` | Azure SQL connection string used by binding | yes      |

Connection string format:

```
Server=<yourserver>.database.windows.net;Database=<db>;User Id=<user>;Password=<pw>;Encrypt=true;
```

---

## Data Sources

- NOAA COOPS – water_level, water_temperature, air_temperature
- USGS NWIS IV – parameter codes 72279 (tidal elevation) and 00010 (water temp)

All timestamps are treated as UTC and inserted verbatim.

---

## Extending the app

- Add more NOAA/USGS stations by updating `USGS_SITES` or `NOAA_STATION` in `function_app.py`.
- Augment the `getStations` function to return additional metadata or filter results.
- Implement additional HTTP triggers for querying observations.

---

## Troubleshooting

- Host start errors: confirm Python version and Azure Functions Core Tools installation.
- SQL connection issues: check firewall/IP rules, connection string syntax.
- API failures: inspect log output in the Functions host for timeouts or parsing errors.