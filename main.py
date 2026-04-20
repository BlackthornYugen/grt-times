import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import ipaddress
import logging
import re
from typing import Optional
from zoneinfo import ZoneInfo
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import httpx
from google.transit import gtfs_realtime_pb2
from google.protobuf.json_format import MessageToDict
import time
import os
import zipfile
import io
import csv
import ssl

_EASTERN = ZoneInfo("America/Toronto")


def _gtfs_datetime_to_iso(start_date: str, start_time: str) -> Optional[str]:
    """Combine GTFS startDate (YYYYMMDD) and startTime (HH:MM:SS) into ISO 8601 with Eastern offset.
    startTime may exceed 24 hours for overnight trips (e.g. '25:30:00')."""
    try:
        base = datetime.strptime(start_date, "%Y%m%d")
        h, m, s = (int(x) for x in start_time.split(":"))
        naive = base + timedelta(hours=h, minutes=m, seconds=s)
        return naive.replace(tzinfo=_EASTERN).isoformat(timespec="seconds")
    except (ValueError, AttributeError):
        return None


def _unix_to_iso(ts) -> Optional[str]:
    """Convert a Unix epoch (int or string) to an ISO 8601 UTC datetime string."""
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OSError):
        return None


def _unix_to_eastern_iso(ts) -> Optional[str]:
    """Convert a Unix epoch (int or string) to an ISO 8601 datetime string with Eastern offset."""
    try:
        return datetime.fromtimestamp(int(ts), tz=_EASTERN).isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError):
        return None


def _make_ssl_context(workaround: bool) -> ssl.SSLContext:
    # GRT's upstream uses a weak DH key rejected by OpenSSL at SECLEVEL=2.
    # SECLEVEL=1 lowers the minimum DH key size to 512 bits.
    # LibreSSL (macOS) ignores @SECLEVEL and doesn't enforce the same restrictions.
    ctx = ssl.create_default_context()
    if workaround:
        try:
            ctx.set_ciphers("DEFAULT@SECLEVEL=1")
        except ssl.SSLError:
            pass
    return ctx


CACHE_TTL_SECONDS = int(os.environ.get("CACHE_TTL_SECONDS", "2"))

_healthcheck_subnet_raw = os.environ.get("SUPPRESS_HEALTHCHECK_LOG_SUBNET", "")
_healthcheck_network = ipaddress.ip_network(_healthcheck_subnet_raw, strict=False) if _healthcheck_subnet_raw else None

# Disable uvicorn's built-in access log; the app middleware handles it with host context.
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


@dataclass
class AgencyConfig:
    name: str
    # Mapping of feed_key → URL. GRT uses two keys ("bus"/"rail"); single-feed agencies use "all".
    vehicle_feed_urls: dict[str, str]
    trip_feed_urls: dict[str, str]
    alerts_url: Optional[str]
    static_gtfs_url: str
    auth_headers: dict[str, str]
    ssl_workaround: bool
    # When True, periodically fetch vehicle feeds to discover route_id → feed_key mappings.
    # When False (single-feed agencies), all routes map to the sole feed key.
    dynamic_route_map: bool


class AgencyState:
    def __init__(self, config: AgencyConfig):
        self.config = config
        self.ssl_context = _make_ssl_context(config.ssl_workaround)
        self.vehicles_cache: dict[str, tuple[dict, float]] = {}
        self.trips_cache: dict[str, tuple[dict, float]] = {}
        self.alerts_cache: dict = {"data": None, "timestamp": 0}
        self.route_feed_map: dict[str, str] = {}
        self.stops_data: dict = {}
        self.static_gtfs_etag: Optional[str] = None
        self.static_gtfs_last_modified: Optional[str] = None

    def resolve_feed_key(self, route_id: str) -> Optional[str]:
        """Return the feed_key for a route, or None if the route is unknown (dynamic mode only)."""
        if self.config.dynamic_route_map:
            return self.route_feed_map.get(route_id)
        return next(iter(self.config.vehicle_feed_urls))


def _build_agencies() -> tuple[list[tuple["re.Pattern[str]", "AgencyState"]], "AgencyState"]:
    _GRT_BASE = "https://webapps.regionofwaterloo.ca/api/grt-routes/api"
    grt = AgencyState(AgencyConfig(
        name="GRT",
        vehicle_feed_urls={
            "bus": f"{_GRT_BASE}/vehiclepositions/1",
            "rail": f"{_GRT_BASE}/vehiclepositions/2",
        },
        trip_feed_urls={
            "bus": f"{_GRT_BASE}/tripupdates/1",
            "rail": f"{_GRT_BASE}/tripupdates/2",
        },
        alerts_url=f"{_GRT_BASE}/alerts",
        static_gtfs_url="https://www.regionofwaterloo.ca/opendatadownloads/GRT_GTFS.zip",
        auth_headers={},
        ssl_workaround=True,
        dynamic_route_map=True,
    ))

    ttc_key = os.environ.get("TRANSIT_TTC_API_KEY", "")
    ttc = AgencyState(AgencyConfig(
        name="TTC",
        vehicle_feed_urls={"all": "https://bustime.ttc.ca/gtfsrt/vehicles"},
        trip_feed_urls={"all": "https://bustime.ttc.ca/gtfsrt/trips"},
        alerts_url="https://bustime.ttc.ca/gtfsrt/alerts",
        static_gtfs_url=(
            "https://ckan0.cf.opendata.inter.prod-toronto.ca/dataset/"
            "7795b45e-e65a-4465-81fc-c36b9dfff169/resource/"
            "cfb6b2b8-6191-41e3-bda1-b175c51148cb/download/"
            "TTC%20Routes%20and%20Schedules%20Data.zip"
        ),
        auth_headers={"Authorization": f"Bearer {ttc_key}"} if ttc_key else {},
        ssl_workaround=False,
        dynamic_route_map=False,
    ))

    oct_key = os.environ.get("TRANSIT_OCT_API_KEY", "")
    oct = AgencyState(AgencyConfig(
        name="OCTranspo",
        vehicle_feed_urls={"all": "https://nextrip-public-api.azure-api.net/octranspo/gtfs-rt-vp/beta/v1/VehiclePositions"},
        trip_feed_urls={"all": "https://nextrip-public-api.azure-api.net/octranspo/gtfs-rt-tp/beta/v1/TripUpdates"},
        alerts_url=None,
        static_gtfs_url="https://oct-gtfs-emasagcnfmcgeham.z01.azurefd.net/public-access/GTFSExport.zip",
        auth_headers={"Ocp-Apim-Subscription-Key": oct_key} if oct_key else {},
        ssl_workaround=False,
        dynamic_route_map=False,
    ))

    # Each entry is (compiled regex, agency state). First match wins.
    # Defaults match any host whose first label is the agency code (grt/ttc/oct).
    registry = [
        (re.compile(os.environ.get("TRANSIT_GRT_HOST_REGEX", r"^grt\.")), grt),
        (re.compile(os.environ.get("TRANSIT_TTC_HOST_REGEX", r"^ttc\.")), ttc),
        (re.compile(os.environ.get("TRANSIT_OCT_HOST_REGEX", r"^oct\.")), oct),
    ]
    return registry, grt


_AGENCY_REGISTRY, _DEFAULT_AGENCY = _build_agencies()

_SERVERS = [
    {"url": "/", "description": "Local/Relative"},
    {"url": os.environ.get("TRANSIT_GRT_SERVER_URL", "https://grt.jskw.ca"), "description": "Grand River Transit"},
    {"url": os.environ.get("TRANSIT_TTC_SERVER_URL", "https://ttc.jskw.ca"), "description": "Toronto Transit Commission"},
    {"url": os.environ.get("TRANSIT_OCT_SERVER_URL", "https://oct.jskw.ca"), "description": "OC Transpo"},
]


def get_agency(request: Request) -> AgencyState:
    host = request.headers.get("host", "").split(":")[0]
    for pattern, state in _AGENCY_REGISTRY:
        if pattern.search(host):
            return state
    return _DEFAULT_AGENCY


async def fetch_route_map(state: AgencyState):
    if not state.config.dynamic_route_map:
        return
    try:
        async with httpx.AsyncClient(verify=state.ssl_context) as client:
            for feed_key, url in state.config.vehicle_feed_urls.items():
                response = await client.get(url, headers=state.config.auth_headers, timeout=10.0)
                if response.status_code == 200:
                    feed = gtfs_realtime_pb2.FeedMessage()
                    feed.ParseFromString(response.content)
                    for entity in MessageToDict(feed).get("entity", []):
                        route_id = entity.get("vehicle", {}).get("trip", {}).get("routeId")
                        if route_id:
                            state.route_feed_map[str(route_id)] = feed_key
    except Exception as e:
        print(f"[{state.config.name}] Error updating route map: {e}")


async def update_route_map_loop(state: AgencyState):
    while True:
        await asyncio.sleep(60)
        await fetch_route_map(state)


async def fetch_static_gtfs(state: AgencyState):
    try:
        async with httpx.AsyncClient(verify=state.ssl_context) as client:
            headers = {}
            if state.static_gtfs_etag:
                headers["If-None-Match"] = state.static_gtfs_etag
            if state.static_gtfs_last_modified:
                headers["If-Modified-Since"] = state.static_gtfs_last_modified

            response = await client.get(state.config.static_gtfs_url, headers=headers, timeout=30.0)

            if response.status_code == 304:
                return

            if response.status_code == 200:
                state.static_gtfs_etag = response.headers.get("ETag")
                state.static_gtfs_last_modified = response.headers.get("Last-Modified")

                with zipfile.ZipFile(io.BytesIO(response.content)) as z:
                    with z.open("stops.txt") as f:
                        reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
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
                                "parentStation": row.get("parent_station"),
                            }
                        if new_stops:
                            state.stops_data.clear()
                            state.stops_data.update(new_stops)
                            print(f"[{state.config.name}] Loaded {len(state.stops_data)} stops from GTFS bundle.")
    except Exception as e:
        print(f"[{state.config.name}] Error updating static GTFS data: {e}")


async def update_static_data_loop(state: AgencyState):
    while True:
        await asyncio.sleep(600)
        await fetch_static_gtfs(state)


@asynccontextmanager
async def lifespan(app: FastAPI):
    tasks = []
    for _, state in _AGENCY_REGISTRY:
        await fetch_route_map(state)
        await fetch_static_gtfs(state)
        tasks.append(asyncio.create_task(update_route_map_loop(state)))
        tasks.append(asyncio.create_task(update_static_data_loop(state)))
    yield
    for task in tasks:
        task.cancel()


tags_metadata = [
    {"name": "Vehicles", "description": "Endpoints regarding real-time vehicle positioning."},
    {"name": "Alerts", "description": "Endpoints regarding active GTFS-RT service alerts."},
    {"name": "Trips", "description": "Endpoints for forecasting vehicle arrivals at stops and schedule deviations."},
    {"name": "Stations", "description": "Endpoints for static definitions of transit hubs, platforms, and specific stops."},
]

app = FastAPI(
    title="Transit API",
    description=(
        "A proxy service that ingests GTFS-Realtime Protocol Buffer feeds from transit agencies "
        "and exposes them as RESTful JSON endpoints.\n\n"
        "This API is designed in accordance with the "
        "[Microsoft Graph REST API Guidelines](https://github.com/microsoft/api-guidelines/blob/vNext/graph/GuidelinesGraph.md)."
    ),
    version="1.0.0",
    contact={
        "name": "John Steel",
        "email": "john@steelcomputers.com",
    },
    servers=_SERVERS,
    openapi_tags=tags_metadata,
    lifespan=lifespan,
    docs_url=None,
)

os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")


LANDING_PAGE_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Transit API</title>
    <link rel="stylesheet" href="/static/landing.css">
</head>
<body>
    <div class="container">
        <span class="badge">v1.0.0</span>
        <h1>Transit API</h1>
        <p>A high-performance proxy service for real-time transit data, providing unified access to GTFS feeds with low-latency caching and standard RESTful responses.</p>
        
        <div class="links">
            <a href="/swagger-ui" class="btn btn-primary">
                <svg class="icon" viewBox="0 0 24 24"><path d="M14,2H6A2,2 0 0,0 4,4V20A2,2 0 0,0 6,22H18A2,2 0 0,0 20,20V8L14,2M18,20H6V4H13V9H18V20M16,11V18.1L13.9,16L11.1,18.8L8.3,16L6.1,18.2V11H16Z" /></svg>
                API Explore
            </a>
            <a href="https://github.com/BlackthornYugen/grt-times" class="btn btn-outline" target="_blank">
                <svg class="icon" viewBox="0 0 24 24"><path d="M12,2A10,10 0 0,0 2,12C2,16.42 4.87,20.17 8.84,21.5C9.34,21.58 9.5,21.27 9.5,21C9.5,20.77 9.5,20.14 9.5,19.31C6.73,19.91 6.14,17.97 6.14,17.97C5.68,16.81 5.03,16.5 5.03,16.5C4.12,15.88 5.1,15.9 5.1,15.9C6.1,15.97 6.63,16.93 6.63,16.93C7.5,18.45 8.97,18 9.54,17.76C9.63,17.11 9.89,16.67 10.17,16.42C7.95,16.17 5.62,15.31 5.62,11.5C5.62,10.39 6,9.5 6.65,8.79C6.55,8.54 6.2,7.5 6.75,6.15C6.75,6.15 7.59,5.88 9.5,7.17C10.29,6.95 11.15,6.84 12,6.84C12.85,6.84 13.71,6.95 14.5,7.17C16.41,5.88 17.25,6.15 17.25,6.15C17.8,7.5 17.45,8.54 17.35,8.79C18,9.5 18.38,10.39 18.38,11.5C18.38,15.32 16.04,16.16 13.81,16.41C14.17,16.72 14.5,17.33 14.5,18.26C14.5,19.6 14.5,20.68 14.5,21C14.5,21.27 14.66,21.59 15.17,21.5C19.14,20.16 22,16.42 22,12A10,10 0 0,0 12,2Z" /></svg>
                GitHub
            </a>
        </div>
    </div>
</body>
</html>
"""


@app.get("/", include_in_schema=False)
async def landing_page():
    return HTMLResponse(content=LANDING_PAGE_HTML)


@app.get("/swagger-ui", include_in_schema=False)
async def custom_swagger_ui_html():
    return get_swagger_ui_html(
        openapi_url=app.openapi_url,
        title=app.title + " - Swagger UI",
        oauth2_redirect_url=app.swagger_ui_oauth2_redirect_url,
        swagger_js_url="/static/swagger-ui/swagger-ui-bundle.js",
        swagger_css_url="/static/swagger-ui/swagger-ui.css",
        swagger_favicon_url="/static/swagger-ui/favicon-32x32.png",
    )

app.add_middleware(
    CORSMiddleware,
    # Reflect the request Origin back so Swagger UI on any agency domain can call
    # any other agency domain. allow_origin_regex causes Starlette to echo the
    # actual Origin header rather than responding with *.
    allow_origin_regex=r".*",
    allow_methods=["GET"],
    allow_headers=["Accept"],
)

_access_log = logging.getLogger("uvicorn")

@app.middleware("http")
async def _security_headers(request: Request, call_next):
    response = await call_next(request)
    csp = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none';"
    )
    response.headers["Content-Security-Policy"] = csp
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    return response


@app.middleware("http")
async def _log_access(request: Request, call_next):
    response = await call_next(request)
    client = request.client.host if request.client else "-"
    method, path = request.method, request.url.path
    if _healthcheck_network and method == "GET" and path == "/openapi.json":
        try:
            if ipaddress.ip_address(client) in _healthcheck_network:
                return response
        except ValueError:
            pass
    host = (request.headers.get("host") or "")[:30]
    _access_log.info('%s - %s - "%s %s" %d', host, client, method, path, response.status_code)
    return response

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
    description="Returns a collection of currently active vehicles specifically assigned to the requested route ID.",
)
async def get_vehicles_by_route(route_id: str, state: AgencyState = Depends(get_agency)):
    feed_key = state.resolve_feed_key(route_id)
    if not feed_key:
        return {"value": []}

    current_time = time.time()
    data = None

    if feed_key in state.vehicles_cache:
        cached_data, timestamp = state.vehicles_cache[feed_key]
        if current_time - timestamp < CACHE_TTL_SECONDS:
            data = cached_data

    if not data:
        url = state.config.vehicle_feed_urls[feed_key]
        try:
            async with httpx.AsyncClient(verify=state.ssl_context) as client:
                response = await client.get(url, headers=state.config.auth_headers, timeout=10.0)
                response.raise_for_status()
                feed = gtfs_realtime_pb2.FeedMessage()
                feed.ParseFromString(response.content)
                fetched_data = MessageToDict(feed)
                state.vehicles_cache[feed_key] = (fetched_data, current_time)
                data = fetched_data
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=f"Error communicating with {state.config.name} API: {e}")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Internal server error: {e}")

    cleaned_entities = []
    for entity in data.get("entity", []):
        vehicle = entity.get("vehicle", {})
        raw_trip = vehicle.get("trip", {})
        if str(raw_trip.get("routeId")) == route_id:
            ts = vehicle.get("timestamp")
            cleaned_entity = {
                "id": entity.get("id"),
                "tripStartDateTime": _gtfs_datetime_to_iso(raw_trip.get("startDate", ""), raw_trip.get("startTime", "")),
                "position": vehicle.get("position"),
                "currentStopSequence": vehicle.get("currentStopSequence"),
                "currentStatus": vehicle.get("currentStatus"),
                "timestamp": _unix_to_iso(ts) if ts else None,
            }
            cleaned_entities.append({k: v for k, v in cleaned_entity.items() if v is not None})

    return {"value": cleaned_entities}


async def get_alerts_data(state: AgencyState) -> list:
    if state.config.alerts_url is None:
        return []

    current_time = time.time()
    if state.alerts_cache["data"] is not None and (current_time - state.alerts_cache["timestamp"]) < CACHE_TTL_SECONDS:
        return state.alerts_cache["data"]

    try:
        async with httpx.AsyncClient(verify=state.ssl_context) as client:
            response = await client.get(state.config.alerts_url, headers=state.config.auth_headers, timeout=10.0)
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
                    "informedEntities": alert_obj.get("informedEntity", []),
                }
                cleaned_alerts.append({k: v for k, v in cleaned_alert.items() if v is not None})

            state.alerts_cache["data"] = cleaned_alerts
            state.alerts_cache["timestamp"] = current_time
            return cleaned_alerts

    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Error communicating with {state.config.name} API: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {e}")


@app.get(
    "/alerts",
    tags=["Alerts"],
    description="Returns a full collection of all currently active transit alerts across the entire network.",
)
async def get_all_alerts(state: AgencyState = Depends(get_agency)):
    return {"value": await get_alerts_data(state)}


@app.get(
    "/routes/{route_id}/alerts",
    tags=["Alerts"],
    description="Returns a collection of active transit alerts filtered dynamically to only ones impacting the requested route ID.",
)
async def get_alerts_by_route(route_id: str, state: AgencyState = Depends(get_agency)):
    alerts = await get_alerts_data(state)
    filtered = [
        a for a in alerts
        if any(str(ie.get("routeId")) == route_id for ie in a.get("informedEntities", []))
    ]
    return {"value": filtered}


@app.get(
    "/routes/{route_id}/trips",
    tags=["Trips"],
    description="Returns an array of ETA updates and stop schedules for all presently active vehicle trips on the requested route ID.",
)
async def get_trips_by_route(route_id: str, state: AgencyState = Depends(get_agency)):
    feed_key = state.resolve_feed_key(route_id)
    if not feed_key:
        return {"value": []}

    current_time = time.time()
    data = None

    if feed_key in state.trips_cache:
        cached_data, timestamp = state.trips_cache[feed_key]
        if current_time - timestamp < CACHE_TTL_SECONDS:
            data = cached_data

    if not data:
        url = state.config.trip_feed_urls[feed_key]
        try:
            async with httpx.AsyncClient(verify=state.ssl_context) as client:
                response = await client.get(url, headers=state.config.auth_headers, timeout=10.0)
                response.raise_for_status()
                feed = gtfs_realtime_pb2.FeedMessage()
                feed.ParseFromString(response.content)
                fetched_data = MessageToDict(feed)
                state.trips_cache[feed_key] = (fetched_data, current_time)
                data = fetched_data
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=f"Error communicating with {state.config.name} API: {e}")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Internal server error: {e}")

    cleaned_entities = []
    for entity in data.get("entity", []):
        trip_update = entity.get("tripUpdate", {})
        raw_trip = trip_update.get("trip", {})

        if str(raw_trip.get("routeId")) == route_id:
            vehicle = trip_update.get("vehicle")
            vehicle_id = vehicle.get("id") if vehicle else None

            stop_updates = []
            for update in trip_update.get("stopTimeUpdate", []):
                stop_id = update.get("stopId")
                stop_data = state.stops_data.get(stop_id or "")

                stop_obj = {"id": stop_id} if stop_id else {}
                if stop_data:
                    stop_obj.update({
                        "name": stop_data.get("name"),
                        "latitude": stop_data.get("latitude"),
                        "longitude": stop_data.get("longitude"),
                    })
                    stop_obj = {k: v for k, v in stop_obj.items() if v is not None}

                def _get_time(event):
                    t = (event or {}).get("time")
                    return _unix_to_eastern_iso(t) if t else None

                entry = {
                    "stopSequence": update.get("stopSequence"),
                    "stop": stop_obj or None,
                    "arrivalTime": _get_time(update.get("arrival")),
                    "departureTime": _get_time(update.get("departure")),
                    "scheduleRelationship": update.get("scheduleRelationship"),
                }
                stop_updates.append({k: v for k, v in entry.items() if v is not None})

            ts = trip_update.get("timestamp")
            cleaned_entity = {
                "id": entity.get("id"),
                "tripStartDateTime": _gtfs_datetime_to_iso(raw_trip.get("startDate", ""), raw_trip.get("startTime", "")),
                "vehicleId": vehicle_id,
                "stopTimeUpdates": stop_updates,
                "timestamp": _unix_to_iso(ts) if ts else None,
            }
            cleaned_entities.append({k: v for k, v in cleaned_entity.items() if v is not None})

    return {"value": cleaned_entities}


@app.get(
    "/stations/{station_id}/arrivals",
    tags=["Trips"],
    description=(
        "Returns upcoming arrivals at a specific stop across all active trips, sorted by arrival time. "
        "Excludes arrivals more than 60 seconds in the past and any stops marked as skipped."
    ),
)
async def get_arrivals_at_station(
    station_id: str,
    top: int = Query(default=10, ge=1, le=100, alias="$top"),
    state: AgencyState = Depends(get_agency),
):
    current_time = time.time()
    arrivals: list[tuple[int, dict]] = []

    for feed_key, url in state.config.trip_feed_urls.items():
        data = None
        if feed_key in state.trips_cache:
            cached_data, timestamp = state.trips_cache[feed_key]
            if current_time - timestamp < CACHE_TTL_SECONDS:
                data = cached_data

        if not data:
            try:
                async with httpx.AsyncClient(verify=state.ssl_context) as client:
                    response = await client.get(url, headers=state.config.auth_headers, timeout=10.0)
                    response.raise_for_status()
                    feed = gtfs_realtime_pb2.FeedMessage()
                    feed.ParseFromString(response.content)
                    data = MessageToDict(feed)
                    state.trips_cache[feed_key] = (data, current_time)
            except httpx.HTTPError as e:
                raise HTTPException(status_code=502, detail=f"Error communicating with {state.config.name} API: {e}")
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"Internal server error: {e}")

        for entity in data.get("entity", []):
            trip_update = entity.get("tripUpdate", {})
            raw_trip = trip_update.get("trip", {})

            for update in trip_update.get("stopTimeUpdate", []):
                if update.get("stopId") != station_id:
                    continue
                if update.get("scheduleRelationship") == "SKIPPED":
                    continue

                arrival_ts = (update.get("arrival") or {}).get("time")
                departure_ts = (update.get("departure") or {}).get("time")
                sort_ts = int(arrival_ts or departure_ts or 0)

                if sort_ts < current_time - 60:
                    continue

                vehicle = trip_update.get("vehicle")
                entry = {
                    "routeId": raw_trip.get("routeId"),
                    "tripId": entity.get("id"),
                    "tripStartDateTime": _gtfs_datetime_to_iso(raw_trip.get("startDate", ""), raw_trip.get("startTime", "")),
                    "vehicleId": vehicle.get("id") if vehicle else None,
                    "arrivalTime": _unix_to_eastern_iso(arrival_ts) if arrival_ts else None,
                    "departureTime": _unix_to_eastern_iso(departure_ts) if departure_ts else None,
                    "scheduleRelationship": update.get("scheduleRelationship"),
                }
                arrivals.append((sort_ts, {k: v for k, v in entry.items() if v is not None}))

    arrivals.sort(key=lambda x: x[0])
    return {"value": [entry for _, entry in arrivals[:top]]}


def _clean_stop(stop: dict) -> dict:
    return {k: v for k, v in stop.items() if v is not None and k != "code"}


@app.get(
    "/stations",
    tags=["Stations"],
    description=(
        "Returns a collection of stops and stations on the transit network. "
        "Defaults to `locationType=0` (individual stops/platforms). "
        "Pass `locationType=1` to retrieve parent station records instead. "
        "Supports `$top` and `$skip` for pagination."
    ),
)
async def get_all_stations(
    top: int = Query(default=100, ge=1, le=1000, alias="$top"),
    skip: int = Query(default=0, ge=0, alias="$skip"),
    location_type: Optional[int] = Query(default=None, alias="locationType"),
    state: AgencyState = Depends(get_agency),
):
    filter_type = location_type if location_type is not None else 0
    stops = [s for s in state.stops_data.values() if s.get("locationType", 0) == filter_type]
    total = len(stops)
    page = stops[skip: skip + top]
    result: dict = {"value": [_clean_stop(s) for s in page]}
    if skip + top < total:
        result["@odata.nextLink"] = f"/stations?$top={top}&$skip={skip + top}"
    return result


@app.get(
    "/stations/{station_id}",
    tags=["Stations"],
    description="Look up a specific station or stop by its unique ID.",
)
async def get_station_by_id(station_id: str, state: AgencyState = Depends(get_agency)):
    stop = state.stops_data.get(station_id)
    if stop:
        return _clean_stop(stop)
    raise HTTPException(status_code=404, detail="Station or stop not found.")
