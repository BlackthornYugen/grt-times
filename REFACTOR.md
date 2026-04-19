# API Refactor Plan

Alignment target: [Microsoft Graph REST API Guidelines](https://github.com/microsoft/api-guidelines/blob/vNext/graph/GuidelinesGraph.md)

---

## Checklist

### Naming

- [x] **Rename `stopTimeUpdate` → `stopTimeUpdates`** (Graph MUST: plural nouns for collection properties)

- [x] **Remove redundant `routeId` from `trip` sub-object** in `/routes/{route_id}/vehicles` and `/routes/{route_id}/trips` responses — always equals the path parameter the client sent.

- [x] **Flatten `vehicle: { "id": "..." }` → `vehicleId: "..."` in trips** — a single-key complex type adds no value over a scalar property.

- [x] **Nest stop enrichment as a `stop` sub-object in `stopTimeUpdates`** — replace the three flat properties (`stopName`, `stopLatitude`, `stopLongitude`) with `stop: { id, name, latitude, longitude }`, consistent with how `position` and `trip` are modeled.

- [x] **Remove `code` field from station responses** — equals `id` on every numeric stop; pure duplication.

---

### Date / Time

- [x] **Combine `trip.startDate` + `trip.startTime` into `trip.departureDateTime` (ISO 8601)**
  - GTFS `startDate` is `YYYYMMDD`; `startTime` is `HH:MM:SS` and **can exceed 24 hours** (e.g. `"25:30:00"` for a trip scheduled 1:30 AM the following service day).
  - Combine by parsing the date, adding the duration represented by `startTime`, then formatting as `"YYYY-MM-DDTHH:MM:SS"`. Do not assume hours < 24.
  - Example: `startDate="20260418"`, `startTime="25:30:00"` → `departureDateTime="2026-04-19T01:30:00"`.

- [x] **Convert `vehicle.timestamp` to ISO 8601 datetime string** — currently a Unix epoch string (`"1776570362"`); should be `"2026-04-18T23:39:22Z"`.

- [x] **Convert `stopTimeUpdate.arrival.time` and `departure.time` to ISO 8601** — same issue; Unix epoch strings inside nested objects.

---

### Error responses

- [x] **Adopt Graph error envelope across all error responses**
  Replace FastAPI's default `{"detail": "..."}` with:
  ```json
  {
    "error": {
      "code": "notFound",
      "message": "Station or stop not found."
    }
  }
  ```
  The `code` value must be the HTTP status description in camelCase (`notFound`, `badGateway`, `internalServerError`). Affects 404, 502, and 500 responses.

---

### Collections

- [x] **Add pagination to `/stations`** — 2,376 items returned in a single payload violates Graph's MUST for `@odata.nextLink` support. Implement `$top` / `$skip` query parameters and include `@odata.nextLink` in the response when more pages exist.

- [x] **Filter `locationType: 1` parent stations from `/stations` by default** — the 14 `place_*` entries are GTFS parent station records that never appear as `stopId` in any real-time feed. Default to returning only `locationType: 0` stops, and expose a `?locationType=` query parameter to opt in to other types.

---

### API Metadata

- [x] **Update API title** — changed from `"GRT Vehicle Positions API"` to `"GRT Transit API"`.

- [x] **Update contact email** — replaced `support@example.com` with real contact.

- [x] **Add Graph guidelines link to API description.**
