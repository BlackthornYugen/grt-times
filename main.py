import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
import httpx
from google.transit import gtfs_realtime_pb2
from google.protobuf.json_format import MessageToDict
import time
import os

BASE_URL = "https://webapps.regionofwaterloo.ca/api/grt-routes/api/vehiclepositions"
CACHE_TTL_SECONDS = int(os.environ.get("CACHE_TTL_SECONDS", "2"))

_cache = {}
_route_type_map = {}

async def fetch_route_map():
    try:
        async with httpx.AsyncClient() as client:
            for vtype in (1, 2):
                response = await client.get(f"{BASE_URL}/{vtype}", timeout=10.0)
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

app = FastAPI(title="GRT Vehicle Positions API", lifespan=lifespan)

@app.get("/")
async def get_root():
    return {"message": "Welcome to the GRT Vehicle Positions Proxy. Try /vehicle/301"}

@app.get("/routes/{route_id}/vehicles")
async def get_vehicles_by_route(route_id: str):
    vehicle_type = _route_type_map.get(route_id)
    
    if not vehicle_type:
        # If route isn't mapped, it might have no vehicles currently
        # Return empty list rather than 404 to gracefully handle routes with no active vehicles
        return {"value": []}
        
    current_time = time.time()
    data = None
    
    if vehicle_type in _cache:
        cached_data, timestamp = _cache[vehicle_type]
        if current_time - timestamp < CACHE_TTL_SECONDS:
            data = cached_data

    if not data:
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(f"{BASE_URL}/{vehicle_type}", timeout=10.0)
                response.raise_for_status()
                
                feed = gtfs_realtime_pb2.FeedMessage()
                feed.ParseFromString(response.content)
                
                fetched_data = MessageToDict(feed)
                _cache[vehicle_type] = (fetched_data, current_time)
                data = fetched_data
        except httpx.HTTPError as http_error:
            raise HTTPException(status_code=502, detail=f"Error communicating with GRT API: {str(http_error)}")
        except Exception as exception:
            raise HTTPException(status_code=500, detail=f"Internal server error: {str(exception)}")

    # Filter entities for the requested route
    filtered_entities = []
    for entity in data.get("entity", []):
        if str(entity.get("vehicle", {}).get("trip", {}).get("routeId")) == route_id:
            filtered_entities.append(entity)

    return {
        "header": data.get("header", {}),
        "value": filtered_entities
    }
