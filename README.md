# GRT Vehicle Positions API

The GRT Vehicle Positions Proxy is a fast, lightweight API built with [FastAPI](https://fastapi.tiangolo.com/) that ingests GTFS-Realtime Protocol Buffer feeds from the Grand River Transit (GRT) system in Waterloo Region and serves slim, cleanly-mapped JSON payloads.

This microservice intelligently classifies GRT routes automatically and models endpoints based on Microsoft Graph API Resource Modeling conventions.

## Getting Started

This project uses [`uv`](https://github.com/astral-sh/uv) to manage dependencies and virtual environments.

### 1. Running the Local Server

You can run the development server easily via Uvicorn. The server will start on port `8000`:
```bash
uv run uvicorn main:app --reload
```

### 2. Available Endpoints

The primary endpoint filters all live vehicle tracking by their active route:

**`GET /routes/{route_id}/vehicles`**

Example output for `/routes/301/vehicles` (The ION Light Rail Train):
```json
{
  "value": [
    {
      "id": "501",
      "trip": {
        "tripId": "387",
        "startTime": "17:00:00",
        "startDate": "20260418",
        "routeId": "301"
      },
      "position": {
        "latitude": 43.496983,
        "longitude": -80.54307
      },
      "currentStopSequence": 15,
      "currentStatus": "IN_TRANSIT_TO",
      "timestamp": "1776548461"
    }
  ]
}
```

## Testing

The project is natively configured for the `pytest` test runner. It includes local mocked unit tests as well as remote integration tests:
```bash
uv run pytest
```

## Linting & API Standards

This API's schema is constructed to respect the Microsoft Azure API Style Guidelines and resource modeling patterns. 
Checkout the [Linting Guide](LINTING.md) inside the repository for instructions on how to use `spectral` to programmatically validate the schema against strict API conventions.
