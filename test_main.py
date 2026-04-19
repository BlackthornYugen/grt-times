import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, AsyncMock, MagicMock
from main import app, _DEFAULT_AGENCY, _gtfs_datetime_to_iso, _unix_to_iso, _unix_to_eastern_iso
import httpx
from google.transit import gtfs_realtime_pb2

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clear_caches():
    """Reset all caches and stop data before each test."""
    _DEFAULT_AGENCY.vehicles_cache.clear()
    _DEFAULT_AGENCY.trips_cache.clear()
    _DEFAULT_AGENCY.alerts_cache.update({"data": None, "timestamp": 0})
    _DEFAULT_AGENCY.stops_data.clear()
    _DEFAULT_AGENCY.route_feed_map.clear()
    yield

@pytest.fixture
def stops_data():
    """Populate stops_data with a few representative entries."""
    _DEFAULT_AGENCY.stops_data.update({
        "1001": {"id": "1001", "name": "King / University", "code": "1001", "latitude": 43.47, "longitude": -80.52, "locationType": 0, "parentStation": ""},
        "1002": {"id": "1002", "name": "Uptown", "code": "1002", "latitude": 43.45, "longitude": -80.49, "locationType": 0, "parentStation": ""},
        "place_KUN": {"id": "place_KUN", "name": "King/University (parent)", "code": "", "latitude": 43.47, "longitude": -80.52, "locationType": 1, "parentStation": ""},
    })
    return _DEFAULT_AGENCY.stops_data


def _make_vehicle_feed(route_id="301", entity_id="1", start_date="20260418", start_time="23:40:00", timestamp=1776570362):
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    entity = feed.entity.add()
    entity.id = entity_id
    entity.vehicle.trip.route_id = route_id
    entity.vehicle.trip.trip_id = "trip_123"
    entity.vehicle.trip.start_date = start_date
    entity.vehicle.trip.start_time = start_time
    entity.vehicle.timestamp = timestamp
    return feed


def _make_trips_feed(route_id="201", entity_id="trip_1", stop_id="1001", arrival_time=1776570240, start_date="20260418", start_time="10:00:00", vehicle_id="512"):
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    entity = feed.entity.add()
    entity.id = entity_id
    entity.trip_update.trip.route_id = route_id
    entity.trip_update.trip.trip_id = "trip_456"
    entity.trip_update.trip.start_date = start_date
    entity.trip_update.trip.start_time = start_time
    entity.trip_update.vehicle.id = vehicle_id
    stu = entity.trip_update.stop_time_update.add()
    stu.stop_sequence = 1
    stu.arrival.time = arrival_time
    stu.stop_id = stop_id
    return feed


def _mock_response(feed):
    mock = AsyncMock()
    mock.status_code = 200
    mock.content = feed.SerializeToString()
    mock.raise_for_status = MagicMock(return_value=None)
    return mock


# ---------------------------------------------------------------------------
# Helper unit tests
# ---------------------------------------------------------------------------

def test_gtfs_datetime_to_iso_normal():
    assert _gtfs_datetime_to_iso("20260418", "23:40:00") == "2026-04-18T23:40:00-04:00"  # EDT

def test_gtfs_datetime_to_iso_overflow():
    """Times ≥ 24h (overnight trips) must roll the date forward."""
    assert _gtfs_datetime_to_iso("20260418", "25:30:00") == "2026-04-19T01:30:00-04:00"  # EDT

def test_gtfs_datetime_to_iso_midnight_boundary():
    assert _gtfs_datetime_to_iso("20260418", "24:00:00") == "2026-04-19T00:00:00-04:00"  # EDT

def test_gtfs_datetime_to_iso_winter():
    """Winter dates use EST offset (-05:00)."""
    assert _gtfs_datetime_to_iso("20260118", "08:00:00") == "2026-01-18T08:00:00-05:00"  # EST

def test_gtfs_datetime_to_iso_invalid():
    assert _gtfs_datetime_to_iso("bad", "also:bad:x") is None

def test_unix_to_iso():
    assert _unix_to_iso(0) == "1970-01-01T00:00:00Z"
    assert _unix_to_iso("1776570362") == "2026-04-19T03:46:02Z"

def test_unix_to_iso_invalid():
    assert _unix_to_iso(None) is None
    assert _unix_to_iso("not_a_number") is None

def test_unix_to_eastern_iso():
    # Unix epoch 0 = 1970-01-01 00:00:00 UTC = 1969-12-31 19:00:00 EST
    assert _unix_to_eastern_iso(0) == "1969-12-31T19:00:00-05:00"

def test_unix_to_eastern_iso_summer():
    # 1776570177 UTC = 2026-04-18 23:42:57 EDT (-04:00)
    assert _unix_to_eastern_iso(1776570177) == "2026-04-18T23:42:57-04:00"

def test_unix_to_eastern_iso_invalid():
    assert _unix_to_eastern_iso(None) is None
    assert _unix_to_eastern_iso("bad") is None


# ---------------------------------------------------------------------------
# Vehicles
# ---------------------------------------------------------------------------

@patch("main.httpx.AsyncClient.get")
@patch.dict(_DEFAULT_AGENCY.route_feed_map, {"301": "rail"})
def test_vehicles_basic_shape(mock_get):
    mock_get.return_value = _mock_response(_make_vehicle_feed())
    data = TestClient(app).get("/routes/301/vehicles").json()
    assert "value" in data
    assert len(data["value"]) == 1

@patch("main.httpx.AsyncClient.get")
@patch.dict(_DEFAULT_AGENCY.route_feed_map, {"301": "rail"})
def test_vehicles_trip_id_not_duplicated(mock_get):
    """tripId must not appear in trip sub-object — it duplicates the top-level id."""
    mock_get.return_value = _mock_response(_make_vehicle_feed(entity_id="1"))
    entity = TestClient(app).get("/routes/301/vehicles").json()["value"][0]
    assert entity["id"] == "1"
    assert "trip" not in entity

@patch("main.httpx.AsyncClient.get")
@patch.dict(_DEFAULT_AGENCY.route_feed_map, {"301": "rail"})
def test_vehicles_no_route_id_in_response(mock_get):
    mock_get.return_value = _mock_response(_make_vehicle_feed())
    entity = TestClient(app).get("/routes/301/vehicles").json()["value"][0]
    assert "routeId" not in entity

@patch("main.httpx.AsyncClient.get")
@patch.dict(_DEFAULT_AGENCY.route_feed_map, {"301": "rail"})
def test_vehicles_trip_start_date_time(mock_get):
    mock_get.return_value = _mock_response(_make_vehicle_feed(start_date="20260418", start_time="23:40:00"))
    entity = TestClient(app).get("/routes/301/vehicles").json()["value"][0]
    assert entity["tripStartDateTime"] == "2026-04-18T23:40:00-04:00"
    assert "startDate" not in entity
    assert "startTime" not in entity

@patch("main.httpx.AsyncClient.get")
@patch.dict(_DEFAULT_AGENCY.route_feed_map, {"301": "rail"})
def test_vehicles_trip_start_date_time_overnight(mock_get):
    mock_get.return_value = _mock_response(_make_vehicle_feed(start_date="20260418", start_time="25:10:00"))
    entity = TestClient(app).get("/routes/301/vehicles").json()["value"][0]
    assert entity["tripStartDateTime"] == "2026-04-19T01:10:00-04:00"

@patch("main.httpx.AsyncClient.get")
@patch.dict(_DEFAULT_AGENCY.route_feed_map, {"301": "rail"})
def test_vehicles_timestamp_is_iso8601(mock_get):
    mock_get.return_value = _mock_response(_make_vehicle_feed(timestamp=0))
    vehicle = TestClient(app).get("/routes/301/vehicles").json()["value"][0]
    assert vehicle["timestamp"] == "1970-01-01T00:00:00Z"


# ---------------------------------------------------------------------------
# Trips
# ---------------------------------------------------------------------------

@patch("main.httpx.AsyncClient.get")
@patch.dict(_DEFAULT_AGENCY.route_feed_map, {"201": "bus"})
def test_trips_basic_shape(mock_get):
    mock_get.return_value = _mock_response(_make_trips_feed())
    data = TestClient(app).get("/routes/201/trips").json()
    assert "value" in data
    assert len(data["value"]) == 1

@patch("main.httpx.AsyncClient.get")
@patch.dict(_DEFAULT_AGENCY.route_feed_map, {"201": "bus"})
def test_trips_stop_time_updates_plural_key(mock_get):
    mock_get.return_value = _mock_response(_make_trips_feed())
    entity = TestClient(app).get("/routes/201/trips").json()["value"][0]
    assert "stopTimeUpdates" in entity
    assert "stopTimeUpdate" not in entity

@patch("main.httpx.AsyncClient.get")
@patch.dict(_DEFAULT_AGENCY.route_feed_map, {"201": "bus"})
def test_trips_trip_id_not_duplicated(mock_get):
    """tripId must not appear in trip sub-object — it duplicates the top-level id."""
    mock_get.return_value = _mock_response(_make_trips_feed())
    entity = TestClient(app).get("/routes/201/trips").json()["value"][0]
    assert entity["id"] == "trip_1"
    assert "tripId" not in entity
    assert "trip" not in entity

@patch("main.httpx.AsyncClient.get")
@patch.dict(_DEFAULT_AGENCY.route_feed_map, {"201": "bus"})
def test_trips_no_route_id_in_response(mock_get):
    mock_get.return_value = _mock_response(_make_trips_feed())
    entity = TestClient(app).get("/routes/201/trips").json()["value"][0]
    assert "routeId" not in entity

@patch("main.httpx.AsyncClient.get")
@patch.dict(_DEFAULT_AGENCY.route_feed_map, {"201": "bus"})
def test_trips_trip_start_date_time(mock_get):
    mock_get.return_value = _mock_response(_make_trips_feed(start_date="20260418", start_time="10:00:00"))
    entity = TestClient(app).get("/routes/201/trips").json()["value"][0]
    assert entity["tripStartDateTime"] == "2026-04-18T10:00:00-04:00"
    assert "startDate" not in entity
    assert "startTime" not in entity

@patch("main.httpx.AsyncClient.get")
@patch.dict(_DEFAULT_AGENCY.route_feed_map, {"201": "bus"})
def test_trips_vehicle_id_flat(mock_get):
    mock_get.return_value = _mock_response(_make_trips_feed(vehicle_id="512"))
    entity = TestClient(app).get("/routes/201/trips").json()["value"][0]
    assert entity["vehicleId"] == "512"
    assert "vehicle" not in entity

@patch("main.httpx.AsyncClient.get")
@patch.dict(_DEFAULT_AGENCY.route_feed_map, {"201": "bus"})
def test_trips_stop_is_nested_object(mock_get, stops_data):
    mock_get.return_value = _mock_response(_make_trips_feed(stop_id="1001"))
    update = TestClient(app).get("/routes/201/trips").json()["value"][0]["stopTimeUpdates"][0]
    assert "stopId" not in update
    assert update["stop"]["id"] == "1001"
    assert update["stop"]["name"] == "King / University"
    assert "latitude" in update["stop"]
    assert "longitude" in update["stop"]

@patch("main.httpx.AsyncClient.get")
@patch.dict(_DEFAULT_AGENCY.route_feed_map, {"201": "bus"})
def test_trips_stop_unknown_id_still_returns_id(mock_get):
    """A stopId not in stops_data should still surface as stop.id."""
    mock_get.return_value = _mock_response(_make_trips_feed(stop_id="unknown_99"))
    update = TestClient(app).get("/routes/201/trips").json()["value"][0]["stopTimeUpdates"][0]
    assert update["stop"]["id"] == "unknown_99"
    assert "name" not in update["stop"]

@patch("main.httpx.AsyncClient.get")
@patch.dict(_DEFAULT_AGENCY.route_feed_map, {"201": "bus"})
def test_trips_arrival_departure_are_flat_eastern(mock_get):
    mock_get.return_value = _mock_response(_make_trips_feed(arrival_time=0))
    update = TestClient(app).get("/routes/201/trips").json()["value"][0]["stopTimeUpdates"][0]
    assert "arrival" not in update
    assert "departure" not in update
    assert update["arrivalTime"] == "1969-12-31T19:00:00-05:00"


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------

@patch("main.httpx.AsyncClient.get")
def test_alerts_basic(mock_get):
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    entity = feed.entity.add()
    entity.id = "alert_1"
    entity.alert.cause = gtfs_realtime_pb2.Alert.Cause.CONSTRUCTION
    entity.alert.effect = gtfs_realtime_pb2.Alert.Effect.DETOUR
    translation = entity.alert.header_text.translation.add()
    translation.text = "Test Stop Closed"
    translation.language = "en"
    ie = entity.alert.informed_entity.add()
    ie.route_id = "301"
    mock_get.return_value = _mock_response(feed)

    client = TestClient(app)
    data = client.get("/alerts").json()
    assert data["value"][0]["id"] == "alert_1"
    assert data["value"][0]["cause"] == "CONSTRUCTION"
    assert data["value"][0]["headerText"] == "Test Stop Closed"

    assert client.get("/routes/301/alerts").json()["value"]
    assert not client.get("/routes/999/alerts").json()["value"]


# ---------------------------------------------------------------------------
# Error responses (Graph envelope)
# ---------------------------------------------------------------------------

def test_error_404_graph_format():
    resp = TestClient(app).get("/stations/does_not_exist_xyz")
    assert resp.status_code == 404
    body = resp.json()
    assert "error" in body
    assert body["error"]["code"] == "notFound"
    assert "message" in body["error"]

@patch("main.httpx.AsyncClient.get")
@patch.dict(_DEFAULT_AGENCY.route_feed_map, {"301": "rail"})
def test_error_502_graph_format(mock_get):
    mock_get.side_effect = httpx.ConnectError("upstream down")
    resp = TestClient(app).get("/routes/301/vehicles")
    assert resp.status_code == 502
    body = resp.json()
    assert body["error"]["code"] == "badGateway"
    assert "message" in body["error"]


# ---------------------------------------------------------------------------
# Stations – pagination, filtering, no code field
# ---------------------------------------------------------------------------

def test_stations_no_code_field(stops_data):
    data = TestClient(app).get("/stations").json()
    for stop in data["value"]:
        assert "code" not in stop

def test_stations_default_returns_only_location_type_0(stops_data):
    data = TestClient(app).get("/stations").json()
    for stop in data["value"]:
        assert stop.get("locationType", 0) == 0

def test_stations_location_type_param_returns_parent_stations(stops_data):
    data = TestClient(app).get("/stations?locationType=1").json()
    assert len(data["value"]) == 1
    assert data["value"][0]["id"] == "place_KUN"

def test_stations_pagination_top_skip(stops_data):
    first = TestClient(app).get("/stations?$top=1&$skip=0").json()
    second = TestClient(app).get("/stations?$top=1&$skip=1").json()
    assert len(first["value"]) == 1
    assert len(second["value"]) == 1
    assert first["value"][0]["id"] != second["value"][0]["id"]

def test_stations_next_link_present_when_more_pages(stops_data):
    data = TestClient(app).get("/stations?$top=1&$skip=0").json()
    assert "@odata.nextLink" in data
    assert "$top=1" in data["@odata.nextLink"]
    assert "$skip=1" in data["@odata.nextLink"]

def test_stations_no_next_link_on_last_page(stops_data):
    data = TestClient(app).get("/stations?$top=100&$skip=0").json()
    assert "@odata.nextLink" not in data

def test_station_by_id_no_code_field(stops_data):
    data = TestClient(app).get("/stations/1001").json()
    assert data["id"] == "1001"
    assert "code" not in data

def test_station_by_id_not_found():
    resp = TestClient(app).get("/stations/does_not_exist_xyz")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# System tests (live GRT API)
# ---------------------------------------------------------------------------

@pytest.mark.system
def test_vehicles_system():
    with TestClient(app) as client:
        resp = client.get("/routes/301/vehicles")
        assert resp.status_code in [200, 502]
        if resp.status_code == 200:
            assert "value" in resp.json()

@pytest.mark.system
def test_alerts_system():
    with TestClient(app) as client:
        resp = client.get("/alerts")
        assert resp.status_code in [200, 502]
        if resp.status_code == 200:
            assert "value" in resp.json()

@pytest.mark.system
def test_trips_system():
    with TestClient(app) as client:
        resp = client.get("/routes/301/trips")
        assert resp.status_code in [200, 502]
        if resp.status_code == 200:
            assert "value" in resp.json()
