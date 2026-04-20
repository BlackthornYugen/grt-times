# Transit API

A fast, lightweight proxy built with [FastAPI](https://fastapi.tiangolo.com/) that ingests GTFS-Realtime Protocol Buffer feeds from multiple transit agencies and serves clean JSON REST responses modeled on Microsoft Graph API conventions.

Supported agencies (routed by `Host` header):

| Host prefix | Agency |
|---|---|
| `grt.*` | Grand River Transit (Waterloo Region) |
| `ttc.*` | Toronto Transit Commission |
| `oct.*` | OC Transpo (Ottawa) |

Unknown hosts fall back to GRT.

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

## Deployment (Helm)

The chart lives in `chart/`. A values override file is the recommended way to deploy — keep it out of version control since it will reference secret names.

### 1. Create the API key secret

TTC and OC Transpo require API keys. Create a Kubernetes Secret **before** installing the chart so Helm never needs to see the values:

```bash
kubectl create secret generic transit-api-secrets \
  --from-literal=TRANSIT_TTC_API_KEY=your_ttc_key \
  --from-literal=TRANSIT_OCT_API_KEY=your_octranspo_key
```

### 2. Create a values override

```yaml
# values.override.yaml
image:
  repository: your-registry/transit-api
  tag: "latest"
  pullPolicy: Always

envFrom:
  - secretRef:
      name: transit-api-secrets

# Optional — override host regex or cache TTL
env:
  - name: CACHE_TTL_SECONDS
    value: "5"
```

### 3. Install / upgrade

```bash
helm upgrade --install grt-times ./chart -f values.override.yaml
```

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `TRANSIT_TTC_API_KEY` | — | TTC feed API key |
| `TRANSIT_OCT_API_KEY` | — | OC Transpo feed API key |
| `TRANSIT_GRT_HOST_REGEX` | `^grt\.` | Regex matched against `Host` to route to GRT |
| `TRANSIT_TTC_HOST_REGEX` | `^ttc\.` | Regex matched against `Host` to route to TTC |
| `TRANSIT_OCT_HOST_REGEX` | `^oct\.` | Regex matched against `Host` to route to OCTranspo |
| `CACHE_TTL_SECONDS` | `2` | Live feed cache TTL in seconds |
| `GTFS_CACHE_DIR` | — | Directory to persist downloaded GTFS zip bundles between restarts. When set, each agency's zip is written to `<dir>/<agency>.zip` alongside a `<agency>.json` sidecar storing the ETag/Last-Modified headers. On startup the cached bundle is loaded immediately, and a conditional HTTP request is made to check for updates. |
| `SSL_CERT_FILE` | — | Path to a PEM CA bundle to trust for outbound HTTPS requests (e.g. `~/.mitmproxy/mitmproxy-ca-cert.pem` when inspecting traffic through a local proxy). Loaded in addition to the system trust store. |
| `UVICORN_FORWARDED_ALLOW_IPS` | `127.0.0.1` | Comma-separated IPs/CIDRs trusted for `X-Forwarded-For` (set to your HAProxy node/pod CIDR) |
| `SUPPRESS_HEALTHCHECK_LOG_SUBNET` | — | CIDR whose `GET /openapi.json` requests are dropped from access logs (e.g. k8s healthcheck subnet) |

## Linting & API Standards

This API's schema is constructed to respect the Microsoft Azure API Style Guidelines and resource modeling patterns. 
Checkout the [Linting Guide](LINTING.md) inside the repository for instructions on how to use `spectral` to programmatically validate the schema against strict API conventions.
