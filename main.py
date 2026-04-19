import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
import httpx
from google.transit import gtfs_realtime_pb2
from google.protobuf.json_format import MessageToDict
import time
import os
import zipfile
import io
import csv
import ssl

# GRT's upstream API uses a weak DH key that OpenSSL rejects at SECLEVEL=2 (default).
# SECLEVEL=1 lowers the minimum DH key size to 512 bits. LibreSSL (macOS) doesn't
# support @SECLEVEL but also doesn't enforce the same restrictions, so we ignore failures.
_ssl_context = ssl.create_default_context()
try:
    _ssl_context.set_ciphers("DEFAULT@SECLEVEL=1")
except ssl.SSLError:
    pass

def _gtfs_datetime_to_iso(start_date: str, start_time: str) -> Optional[str]:
    """Combine GTFS startDate (YYYYMMDD) and startTime (HH:MM:SS) into ISO 8601.
    startTime may exceed 24 hours for overnight trips (e.g. '25:30:00')."""
    try:
        base = datetime.strptime(start_date, "%Y%m%d")
        h, m, s = (int(x) for x in start_time.split(":"))
        return (base + timedelta(hours=h, minutes=m, seconds=s)).strftime("%Y-%m-%dT%H:%M:%S")
    except (ValueError, AttributeError):
        return None

def _unix_to_iso(ts) -> Optional[str]:
    """Convert a Unix epoch (int or string) to an ISO 8601 UTC datetime string."""
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OSError):
        return None

VEHICLES_URL = "https://webapps.regionofwaterloo.ca/api/grt-routes/api/vehiclepositions"
ALERTS_URL = "https://webapps.regionofwaterloo.ca/api/grt-routes/api/alerts"
TRIPS_URL = "https://webapps.regionofwaterloo.ca/api/grt-routes/api/tripupdates"
STATIC_GTFS_URL = "https://www.regionofwaterloo.ca/opendatadownloads/GRT_GTFS.zip"
CACHE_TTL_SECONDS = int(os.environ.get("CACHE_TTL_SECONDS", "2"))

_vehicles_cache = {}
_trips_cache = {}
_alerts_cache = {"data": None, "timestamp": 0}
_route_type_map = {}
_stops_data = {}
_static_gtfs_etag = None
_static_gtfs_last_modified = None

async def fetch_route_map():
    try:
        async with httpx.AsyncClient(verify=_ssl_context) as client:
            for vtype in (1, 2):
                response = await client.get(f"{VEHICLES_URL}/{vtype}", timeout=10.0)
                if response.status_code == 200:
                    feed = gtfs_realtime_pb2.FeedMessage()
                    feed.ParseFromString(response.content)
                    data = MessageToDict(feed)
                    for entity in data.get("entity", []):
                        route_id = entity.get("vehicle", {}).get("trip", {}).get("routeId")
                        if route_id:
                            _route_type_map[str(route_id)] = vtype
    except Exception as e:
        print(f"Error updating route map: {e}")

async def update_route_map_loop():
    while True:
        await asyncio.sleep(60)
        await fetch_route_map()

async def fetch_static_gtfs():
    global _static_gtfs_etag, _static_gtfs_last_modified, _stops_data
    try:
        async with httpx.AsyncClient(verify=_ssl_context) as client:
            headers = {}
            if _static_gtfs_etag:
                headers["If-None-Match"] = _static_gtfs_etag
            if _static_gtfs_last_modified:
                headers["If-Modified-Since"] = _static_gtfs_last_modified
            
            response = await client.get(STATIC_GTFS_URL, headers=headers, timeout=30.0)
            
            if response.status_code == 304:
                return # Not modified, nothing to do
                
            if response.status_code == 200:
                _static_gtfs_etag = response.headers.get("ETag")
                _static_gtfs_last_modified = response.headers.get("Last-Modified")
                
                with zipfile.ZipFile(io.BytesIO(response.content)) as z:
                    with z.open("stops.txt") as f:
                        text_wrapper = io.TextIOWrapper(f, encoding='utf-8-sig') # Using utf-8-sig for BOM handling
                        reader = csv.DictReader(text_wrapper)
                        new_stops = {}
                        for row in reader:
                            stop_id = row.get("stop_id")
                            new_stops[stop_id] = {
                                "id": stop_id,
                                "name": row.get("stop_name"),
                                "code": row.get("stop_code"),
                                "latitude": float(row.get("stop_lat")) if row.get("stop_lat") else None,
                                "longitude": float(row.get("stop_lon")) if row.get("stop_lon") else None,
                                "locationType": int(row.get("location_type")) if row.get("location_type") else 0,
                                "parentStation": row.get("parent_station")
                            }
                        if new_stops:
                            _stops_data.clear()
                            _stops_data.update(new_stops)
                            print(f"Successfully loaded {len(_stops_data)} stops/stations from GTFS bundle.")
                            
    except Exception as e:
        print(f"Error updating static GTFS data: {e}")

async def update_static_data_loop():
    while True:
        await asyncio.sleep(600) # Every 10 minutes
        await fetch_static_gtfs()

@asynccontextmanager
async def lifespan(app: FastAPI):
    await fetch_route_map()
    await fetch_static_gtfs()
    task1 = asyncio.create_task(update_route_map_loop())
    task2 = asyncio.create_task(update_static_data_loop())
    yield
    task1.cancel()
    task2.cancel()

tags_metadata = [
    {
        "name": "Vehicles",
        "description": "Endpoints regarding real-time vehicle positioning."
    },
    {
        "name": "Alerts",
        "description": "Endpoints regarding active GTFS-RT service alerts."
    },
    {
        "name": "Trips",
        "description": "Endpoints for forecasting vehicle arrivals at stops and schedule deviations."
    },
    {
        "name": "Stations",
        "description": "Endpoints for static definitions of transit hubs, platforms, and specific stops."
    }
]

app = FastAPI(
    title="GRT Transit API",
    description=(
        "A proxy service that ingests GTFS-Realtime Protocol Buffer feeds from Grand River Transit (GRT) "
        "and exposes them as RESTful JSON endpoints.\n\n"
        "This API is designed in accordance with the "
        "[Microsoft Graph REST API Guidelines](https://github.com/microsoft/api-guidelines/blob/vNext/graph/GuidelinesGraph.md)."
    ),
    version="1.0.0",
    contact={
        "name": "John Steel",
        "email": "john@steelcomputers.com"
    },
    servers=[
        {"url": "/", "description": "Local/Relative Server"}
    ],
    openapi_tags=tags_metadata,
    lifespan=lifespan,
    docs_url="/"
)

_HTTP_STATUS_CODES = {
    400: "badRequest", 404: "notFound", 429: "tooManyRequests",
    500: "internalServerError", 502: "badGateway", 503: "serviceUnavailable",
}

@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc: HTTPException):
    code = _HTTP_STATUS_CODES.get(exc.status_code, "error")
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": code, "message": exc.detail}},
    )

@app.get(
    "/routes/{route_id}/vehicles",
    tags=["Vehicles"],
    description="Returns a collection of currently active vehicles specifically assigned to the requested route ID."
)
async def get_vehicles_by_route(route_id: str):
    vehicle_type = _route_type_map.get(route_id)
    
    if not vehicle_type:
        # If route isn't mapped, it might have no vehicles currently
        # Return empty list rather than 404 to gracefully handle routes with no active vehicles
        return {"value": []}
        
    current_time = time.time()
    data = None
    
    if vehicle_type in _vehicles_cache:
        cached_data, timestamp = _vehicles_cache[vehicle_type]
        if current_time - timestamp < CACHE_TTL_SECONDS:
            data = cached_data

    if not data:
        try:
            async with httpx.AsyncClient(verify=_ssl_context) as client:
                response = await client.get(f"{VEHICLES_URL}/{vehicle_type}", timeout=10.0)
                response.raise_for_status()
                
                feed = gtfs_realtime_pb2.FeedMessage()
                feed.ParseFromString(response.content)
                
                fetched_data = MessageToDict(feed)
                _vehicles_cache[vehicle_type] = (fetched_data, current_time)
                data = fetched_data
        except httpx.HTTPError as http_error:
            raise HTTPException(status_code=502, detail=f"Error communicating with GRT API: {str(http_error)}")
        except Exception as exception:
            raise HTTPException(status_code=500, detail=f"Internal server error: {str(exception)}")

    cleaned_entities = []
    for entity in data.get("entity", []):
        vehicle = entity.get("vehicle", {})
        raw_trip = vehicle.get("trip", {})

        if str(raw_trip.get("routeId")) == route_id:
            cleaned_trip = {k: v for k, v in raw_trip.items() if k not in ("routeId", "startDate", "startTime")}
            iso_dt = _gtfs_datetime_to_iso(raw_trip.get("startDate", ""), raw_trip.get("startTime", ""))
            if iso_dt:
                cleaned_trip["departureDateTime"] = iso_dt

            ts = vehicle.get("timestamp")
            cleaned_entity = {
                "id": entity.get("id"),
                "trip": cleaned_trip or None,
                "position": vehicle.get("position"),
                "currentStopSequence": vehicle.get("currentStopSequence"),
                "currentStatus": vehicle.get("currentStatus"),
                "timestamp": _unix_to_iso(ts) if ts else None,
            }
            cleaned_entities.append({k: v for k, v in cleaned_entity.items() if v is not None})

    return {"value": cleaned_entities}

async def get_alerts_data():
    current_time = time.time()
    
    if _alerts_cache["data"] is not None and (current_time - _alerts_cache["timestamp"]) < CACHE_TTL_SECONDS:
        return _alerts_cache["data"]
        
    try:
        async with httpx.AsyncClient(verify=_ssl_context) as client:
            response = await client.get(ALERTS_URL, timeout=10.0)
            response.raise_for_status()
            
            feed = gtfs_realtime_pb2.FeedMessage()
            feed.ParseFromString(response.content)
            fetched_data = MessageToDict(feed)
            
            cleaned_alerts = []
            for entity in fetched_data.get("entity", []):
                alert_obj = entity.get("alert", {})
                
                header_text = None
                translations = alert_obj.get("headerText", {}).get("translation", [])
                for t in translations:
                    if t.get("language") == "en":
                        header_text = t.get("text")
                        break
                if not header_text and translations:
                    header_text = translations[0].get("text")
                    
                cleaned_alert = {
                    "id": entity.get("id"),
                    "cause": alert_obj.get("cause"),
                    "effect": alert_obj.get("effect"),
                    "headerText": header_text,
                    "informedEntities": alert_obj.get("informedEntity", [])
                }
                cleaned_alert = {k: v for k, v in cleaned_alert.items() if v is not None}
                cleaned_alerts.append(cleaned_alert)
                
            _alerts_cache["data"] = cleaned_alerts
            _alerts_cache["timestamp"] = current_time
            return cleaned_alerts
            
    except httpx.HTTPError as http_error:
        raise HTTPException(status_code=502, detail=f"Error communicating with GRT API: {str(http_error)}")
    except Exception as exception:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(exception)}")

@app.get(
    "/alerts",
    tags=["Alerts"],
    description="Returns a full collection of all currently active transit alerts across the entire GRT network."
)
async def get_all_alerts():
    alerts = await get_alerts_data()
    return {"value": alerts}

@app.get(
    "/routes/{route_id}/alerts",
    tags=["Alerts"],
    description="Returns a collection of active transit alerts filtered dynamically to only ones impacting the requested route ID."
)
async def get_alerts_by_route(route_id: str):
    alerts = await get_alerts_data()
    
    filtered_alerts = []
    for alert in alerts:
        # Check if route_id is in any of the informedEntities
        informed_entities = alert.get("informedEntities", [])
        if any(str(ie.get("routeId")) == route_id for ie in informed_entities):
            filtered_alerts.append(alert)
            
    return {"value": filtered_alerts}

@app.get(
    "/routes/{route_id}/trips",
    tags=["Trips"],
    description="Returns an array of ETA updates and stop schedules for all presently active vehicle trips on the requested route ID."
)
async def get_trips_by_route(route_id: str):
    vehicle_type = _route_type_map.get(route_id)
    if not vehicle_type:
        return {"value": []}

    current_time = time.time()
    data = None
    
    if vehicle_type in _trips_cache:
        cached_data, timestamp = _trips_cache[vehicle_type]
        if current_time - timestamp < CACHE_TTL_SECONDS:
            data = cached_data

    if not data:
        try:
            async with httpx.AsyncClient(verify=_ssl_context) as client:
                response = await client.get(f"{TRIPS_URL}/{vehicle_type}", timeout=10.0)
                response.raise_for_status()
                
                feed = gtfs_realtime_pb2.FeedMessage()
                feed.ParseFromString(response.content)
                
                fetched_data = MessageToDict(feed)
                _trips_cache[vehicle_type] = (fetched_data, current_time)
                data = fetched_data
        except httpx.HTTPError as http_error:
            raise HTTPException(status_code=502, detail=f"Error communicating with GRT API: {str(http_error)}")
        except Exception as exception:
            raise HTTPException(status_code=500, detail=f"Internal server error: {str(exception)}")

    cleaned_entities = []
    for entity in data.get("entity", []):
        trip_update = entity.get("tripUpdate", {})
        raw_trip = trip_update.get("trip", {})

        if str(raw_trip.get("routeId")) == route_id:
            # Build trip sub-object: remove routeId, combine startDate+startTime
            cleaned_trip = {k: v for k, v in raw_trip.items() if k not in ("routeId", "startDate", "startTime")}
            iso_dt = _gtfs_datetime_to_iso(raw_trip.get("startDate", ""), raw_trip.get("startTime", ""))
            if iso_dt:
                cleaned_trip["departureDateTime"] = iso_dt

            # Flatten vehicle object to vehicleId scalar
            vehicle = trip_update.get("vehicle")
            vehicle_id = vehicle.get("id") if vehicle else None

            # Build enriched stop time updates
            stop_updates = []
            for update in trip_update.get("stopTimeUpdate", []):
                stop_id = update.get("stopId")
                stop_data = _stops_data.get(stop_id or "")

                stop_obj = {"id": stop_id} if stop_id else {}
                if stop_data:
                    stop_obj.update({
                        "name": stop_data.get("name"),
                        "latitude": stop_data.get("latitude"),
                        "longitude": stop_data.get("longitude"),
                    })
                    stop_obj = {k: v for k, v in stop_obj.items() if v is not None}

                def convert_time_entry(entry):
                    if entry and "time" in entry:
                        iso = _unix_to_iso(entry["time"])
                        return {**entry, "time": iso or entry["time"]}
                    return entry

                entry = {
                    "stopSequence": update.get("stopSequence"),
                    "stop": stop_obj or None,
                    "arrival": convert_time_entry(update.get("arrival")),
                    "departure": convert_time_entry(update.get("departure")),
                    "scheduleRelationship": update.get("scheduleRelationship"),
                }
                stop_updates.append({k: v for k, v in entry.items() if v is not None})

            ts = trip_update.get("timestamp")
            cleaned_entity = {
                "id": entity.get("id"),
                "trip": cleaned_trip or None,
                "vehicleId": vehicle_id,
                "stopTimeUpdates": stop_updates,
                "timestamp": _unix_to_iso(ts) if ts else None,
            }
            cleaned_entities.append({k: v for k, v in cleaned_entity.items() if v is not None})

    return {"value": cleaned_entities}

def _clean_stop(stop: dict) -> dict:
    return {k: v for k, v in stop.items() if v is not None and k != "code"}

@app.get(
    "/stations",
    tags=["Stations"],
    description=(
        "Returns a collection of stops and stations on the GRT network. "
        "Defaults to `locationType=0` (individual stops/platforms). "
        "Pass `locationType=1` to retrieve parent station records instead. "
        "Supports `$top` and `$skip` for pagination."
    ),
)
async def get_all_stations(
    top: int = Query(default=100, ge=1, le=1000, alias="$top"),
    skip: int = Query(default=0, ge=0, alias="$skip"),
    location_type: Optional[int] = Query(default=None, alias="locationType"),
):
    filter_type = location_type if location_type is not None else 0
    stops = [s for s in _stops_data.values() if s.get("locationType", 0) == filter_type]

    total = len(stops)
    page = stops[skip: skip + top]

    result: dict = {"value": [_clean_stop(s) for s in page]}
    if skip + top < total:
        result["@odata.nextLink"] = f"/stations?$top={top}&$skip={skip + top}"
    return result

@app.get(
    "/stations/{station_id}",
    tags=["Stations"],
    description="Look up a specific station or stop by its unique ID."
)
async def get_station_by_id(station_id: str):
    stop = _stops_data.get(station_id)
    if stop:
        return _clean_stop(stop)
    raise HTTPException(status_code=404, detail="Station or stop not found.")
