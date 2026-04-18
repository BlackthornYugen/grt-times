import asyncio
import httpx
from google.transit import gtfs_realtime_pb2
from google.protobuf.json_format import MessageToDict
import json

BASE_URL = "https://webapps.regionofwaterloo.ca/api/grt-routes/api/vehiclepositions"

async def main():
    async with httpx.AsyncClient() as client:
        r2 = await client.get(f"{BASE_URL}/2")
        feed = gtfs_realtime_pb2.FeedMessage()
        feed.ParseFromString(r2.content)
        data = MessageToDict(feed)
        print(json.dumps(data.get("entity", [])[0], indent=2))
        
asyncio.run(main())
