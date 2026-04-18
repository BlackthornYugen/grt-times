import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
import httpx
from google.transit import gtfs_realtime_pb2
from google.protobuf.json_format import MessageToDict
import time
import os

VEHICLES_URL = "https://webapps.regionofwaterloo.ca/api/grt-routes/api/vehiclepositions"
ALERTS_URL = "https://webapps.regionofwaterloo.ca/api/grt-routes/api/alerts"
CACHE_TTL_SECONDS = int(os.environ.get("CACHE_TTL_SECONDS", "2"))

_vehicles_cache = {}
_alerts_cache = {"data": None, "timestamp": 0}
_route_type_map = {}

async def fetch_route_map():
    try:
        async with httpx.AsyncClient() as client:
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

@asynccontextmanager
async def lifespan(app: FastAPI):
    await fetch_route_map()
    task = asyncio.create_task(update_route_map_loop())
    yield
    task.cancel()

tags_metadata = [
    {
        "name": "Root",
        "description": "Core routing and entry points."
    },
    {
        "name": "Vehicles",
        "description": "Endpoints regarding real-time vehicle positioning."
    },
    {
        "name": "Alerts",
        "description": "Endpoints regarding active GTFS-RT service alerts."
    }
]

app = FastAPI(
    title="GRT Vehicle Positions API",
    description="A proxy service that ingest GTFS-Realtime Protocol Buffer feeds from Grand River Transit (GRT) and exposes them as RESTful JSON endpoints.",
    version="1.0.0",
    contact={
        "name": "API Support",
        "email": "support@example.com"
    },
    servers=[
        {"url": "/", "description": "Local/Relative Server"}
    ],
    openapi_tags=tags_metadata,
    lifespan=lifespan
)

@app.get(
    "/",
    tags=["Root"],
    description="Root endpoint welcoming users and providing a basic hint on how to use the API."
)
async def get_root():
    return {"message": "Welcome to the GRT Vehicle Positions Proxy. Try /routes/301/vehicles"}

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
            async with httpx.AsyncClient() as client:
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

    # Clean and filter entities for the requested route
    cleaned_entities = []
    for entity in data.get("entity", []):
        vehicle = entity.get("vehicle", {})
        trip = vehicle.get("trip", {})
        
        if str(trip.get("routeId")) == route_id:
            cleaned_entity = {
                "id": entity.get("id"),
                "trip": trip,
                "position": vehicle.get("position"),
                "currentStopSequence": vehicle.get("currentStopSequence"),
                "currentStatus": vehicle.get("currentStatus"),
                "timestamp": vehicle.get("timestamp")
            }
            # Optional: Remove any keys that are None to keep it ultra clean
            cleaned_entity = {k: v for k, v in cleaned_entity.items() if v is not None}
            cleaned_entities.append(cleaned_entity)

    return {
        "value": cleaned_entities
    }

async def get_alerts_data():
    current_time = time.time()
    
    if _alerts_cache["data"] is not None and (current_time - _alerts_cache["timestamp"]) < CACHE_TTL_SECONDS:
        return _alerts_cache["data"]
        
    try:
        async with httpx.AsyncClient() as client:
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
