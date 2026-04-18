import asyncio
import httpx
from google.transit import gtfs_realtime_pb2
from google.protobuf.json_format import MessageToDict

BASE_URL = "https://webapps.regionofwaterloo.ca/api/grt-routes/api/vehiclepositions"

async def main():
    async with httpx.AsyncClient() as client:
        r2 = await client.get(f"{BASE_URL}/2")
        feed = gtfs_realtime_pb2.FeedMessage()
        feed.ParseFromString(r2.content)
        data = MessageToDict(feed)
        
        routes = set()
        for entity in data.get("entity", []):
            trip_desc = entity.get("vehicle", {}).get("trip", {})
            if "routeId" in trip_desc:
                routes.add(trip_desc["routeId"])
        print("Type 2 routes:", routes)

        r1 = await client.get(f"{BASE_URL}/1")
        feed1 = gtfs_realtime_pb2.FeedMessage()
        feed1.ParseFromString(r1.content)
        data1 = MessageToDict(feed1)
        
        routes1 = set()
        for entity in data1.get("entity", []):
            trip_desc = entity.get("vehicle", {}).get("trip", {})
            if "routeId" in trip_desc:
                routes1.add(trip_desc["routeId"])
        print("Type 1 routes size:", len(routes1))
        
asyncio.run(main())
