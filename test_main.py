import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, AsyncMock, MagicMock
from main import app
import httpx
from google.transit import gtfs_realtime_pb2

@pytest.fixture(autouse=True)
def clear_caches():
    """Ensure caches are empty before each test so we don't bleed mock data into system tests."""
    from main import _vehicles_cache, _alerts_cache
    _vehicles_cache.clear()
    _alerts_cache.update({"data": None, "timestamp": 0})
    yield

def test_read_root():
    """Unit test for the root endpoint."""
    client = TestClient(app)
    response = client.get("/")
    assert response.status_code == 200
    assert response.json() == {"message": "Welcome to the GRT Vehicle Positions Proxy. Try /routes/301/vehicles"}

@patch("main.httpx.AsyncClient.get")
@patch.dict("main._route_type_map", {"301": 2})
def test_read_vehicle_unit(mock_get):
    """Unit test for the vehicle route endpoint using mocked GRT API response."""
    # Create fake protobuf response
    feed = gtfs_realtime_pb2.FeedMessage()
    header = feed.header
    header.gtfs_realtime_version = "2.0"
    
    entity = feed.entity.add()
    entity.id = "1"
    entity.vehicle.trip.route_id = "301"

    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.content = feed.SerializeToString()
    mock_response.raise_for_status = MagicMock(return_value=None)
    
    mock_get.return_value = mock_response

    client = TestClient(app)
    response = client.get("/routes/301/vehicles")
    assert response.status_code == 200
    data = response.json()
    assert "header" not in data
    assert "value" in data
    assert len(data["value"]) == 1
    assert data["value"][0]["id"] == "1"
    assert data["value"][0]["trip"]["routeId"] == "301"

@patch("main.httpx.AsyncClient.get")
@patch.dict("main._route_type_map", {"301": 2})
def test_read_alerts_unit(mock_get):
    """Unit test for alerts endpoints."""
    # Create fake protobuf response
    feed = gtfs_realtime_pb2.FeedMessage()
    header = feed.header
    header.gtfs_realtime_version = "2.0"
    
    entity = feed.entity.add()
    entity.id = "alert_1"
    entity.alert.cause = gtfs_realtime_pb2.Alert.Cause.CONSTRUCTION
    entity.alert.effect = gtfs_realtime_pb2.Alert.Effect.DETOUR
    
    translation = entity.alert.header_text.translation.add()
    translation.text = "Test Stop Closed"
    translation.language = "en"
    
    ie = entity.alert.informed_entity.add()
    ie.route_id = "301"

    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.content = feed.SerializeToString()
    mock_response.raise_for_status = MagicMock(return_value=None)
    
    mock_get.return_value = mock_response

    client = TestClient(app)
    
    # Test all alerts
    response = client.get("/alerts")
    assert response.status_code == 200
    data = response.json()
    assert "value" in data
    assert len(data["value"]) == 1
    assert data["value"][0]["id"] == "alert_1"
    assert data["value"][0]["cause"] == "CONSTRUCTION"
    assert data["value"][0]["headerText"] == "Test Stop Closed"

    # Test routes/{route_id}/alerts
    response = client.get("/routes/301/alerts")
    assert response.status_code == 200
    data = response.json()
    assert len(data["value"]) == 1

    # Test unrelated route alerts
    response = client.get("/routes/999/alerts")
    assert response.status_code == 200
    assert len(response.json()["value"]) == 0

@pytest.mark.system
def test_read_vehicle_system():
    """System test that hits the actual GRT API."""
    with TestClient(app) as client:
        response = client.get("/routes/301/vehicles")
        
        assert response.status_code in [200, 502]
        
        if response.status_code == 200:
            data = response.json()
            assert "header" not in data
            assert "value" in data

@pytest.mark.system
def test_read_alerts_system():
    """System test for alerts."""
    with TestClient(app) as client:
        response = client.get("/alerts")
        assert response.status_code in [200, 502]
        if response.status_code == 200:
            assert "value" in response.json()
