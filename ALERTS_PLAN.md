# API Expansion Plan: GRT Alerts

Currently, the GTFS Realtime alerts feed (`https://webapps.regionofwaterloo.ca/api/grt-routes/api/alerts`) at GRT returns a standard protocol buffer containing `Alert` entities. The feed size is small (often 15 bytes when no alerts are active), meaning we can fetch it dynamically or cache it similarly to the vehicle positions. 

To expand our API gracefully while adhering to the **Microsoft Graph Resource Modeling guidelines** we established, here is the implementation plan:

## 1. Network & Caching Strategy
- **New Source**: Define `ALERTS_URL = "https://webapps.regionofwaterloo.ca/api/grt-routes/api/alerts"` in `main.py`.
- **Caching**: Introduce a new `_alerts_cache` (caching the parsed payload alongside a `timestamp`), using the identical `CACHE_TTL_SECONDS` default of 2 seconds. Because alerts update infrequently, we could optionally scale this TTL specifically for alerts context later.

## 2. New Endpoint Definitions
Following the Microsoft API Guidelines resource mapping (plural collections and navigation properties), we will add two endpoints:

### Endpoint A: `/alerts`
- **Purpose**: Returns *all* active service alerts across the transit network (including system-wide disruptions, stop closures, and general bulletins).
- **Structure**: A top-level collection endpoint mapping perfectly to standard API paradigms.

### Endpoint B: `/routes/{route_id}/alerts`
- **Purpose**: Returns only the service alerts that specifically impact the requested route. 
- **Filtering Logic**: Evaluates the `informedEntity` array inside each GTFS alert to see if any object in the array explicitly references `"routeId": "{route_id}"`.

## 3. Data Flattening & Cleanup
Similar to how we stripped out the nested `vehicle > trip` structures for vehicles, GTFS Service Alerts bury the actual human-readable messages under localization dicts (e.g., `alert.headerText.translation[0].text`). 

The logic will extract and flatten these structures into a clean JSON interface:

**Raw GTFS Representation:**
```json
{
  "id": "alert_101",
  "alert": {
    "informedEntity": [{ "routeId": "301" }],
    "cause": "CONSTRUCTION",
    "effect": "DETOUR",
    "headerText": { "translation": [{ "text": "Station closed", "language": "en" }] }
  }
}
```

**Cleaned API Payload we will produce:**
```json
{
  "value": [
    {
      "id": "alert_101",
      "cause": "CONSTRUCTION",
      "effect": "DETOUR",
      "headerText": "Station closed",
      "informedEntities": [
        { "routeId": "301" }
      ]
    }
  ]
}
```

## 4. Updates to Testing & Linting
- Create mock GTFS-RT `Alert` protobuf feeds in `test_main.py` mirroring the unit tests for vehicles.
- No changes required to `.spectral.yaml`, but we will ensure Spectral validations continuously pass on the newly generated API spec.

---

*If you approve this plan, I'll go ahead and build it into `main.py`!*
