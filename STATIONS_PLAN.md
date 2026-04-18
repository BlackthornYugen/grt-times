# API Expansion Plan: GRT Stations (Static GTFS)

Up until now, we've strictly relied on the real-time protobuf feeds (`vehiclepositions`, `tripupdates`, `alerts`). However, those APIs explicitly **do not** contain any metadata about the physical stops or stations—they only yield raw `stopId` numbers!

To build an API for stations, we need to transition into **GTFS Static** territory. 

## 1. The Data Source
The Region of Waterloo continuously updates a `.zip` artifact at `https://www.regionofwaterloo.ca/opendatadownloads/GRT_GTFS.zip` containing scheduled routing info and physical station coordinates.

## 2. Ingestion & Server Lifespan Logic
Instead of querying it continuously, we should:
- Download `GRT_GTFS.zip` directly into memory or a temp file during the `lifespan` FastAPI startup.
- Extract the `stops.txt` (a CSV spreadsheet containing all stops, ION rail stations, and bus terminals).
- Filter out standard bus stops if we strictly only want major `stations` (In standard GTFS, `location_type=1` represents a parent Station vs `0` for platforms/stops)—or keep all of them depending on your preference!
- Cache this deeply into a static memory dictionary keyed by `stop_id`.

## 3. Endpoints (MS Graph Alignment)
*   `GET /stations`: Returns a master list of all stations.
*   `GET /stations/{station_id}`: Lookups up naming, latitude, and longitude for a given ID.

Because parsing CSV files adds additional dependencies (like Python's `csv` module), it's a slight shift in how our app behaves! Does this downloading-and-caching strategy sound like how you want to approach it, or should we expose *all* stops and name the endpoint `/stops`? 
