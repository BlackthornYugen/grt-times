import json
from main import app

# Export the openapi.json file
with open("openapi.json", "w") as f:
    json.dump(app.openapi(), f, indent=2)

print("openapi.json exported successfully!")
