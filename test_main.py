import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, AsyncMock, MagicMock
from main import app
import httpx
from google.transit import gtfs_realtime_pb2

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

@pytest.mark.system
def test_read_vehicle_system():
    """System test that hits the actual GRT API."""
    with TestClient(app) as client:
        response = client.get("/routes/301/vehicles")
        
        # Check if the service returns data successfully (or returns a proper proxy error like 502)
        assert response.status_code in [200, 502]
        
        if response.status_code == 200:
            data = response.json()
            assert "header" not in data
            assert "value" in data
