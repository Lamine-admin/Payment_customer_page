import requests
from decouple import config

# Configuration API Fleetee
FLEETEE_API_BASE = "https://api.fleetee.io"
fleetee_api_key = config('FLEETEE_API_KEY')
fleetee_secret_key = config('FLEETEE_SECRET_KEY')

def fleetee_api_get(endpoint, params=None):
    headers = {
        "x-api-key": fleetee_api_key,
        "secret-key": fleetee_secret_key
    }
    url = f"{FLEETEE_API_BASE}{endpoint}"
    response = requests.get(url, headers=headers, params=params)
    if response.status_code == 200:
        return response.json()
    else:
        print(f"Erreur API Fleetee: {response.status_code} {response.text}")
        return None

def list_all_vehicles():
    vehicles = []
    offset = 0
    page_size = 20  # selon la pagination Fleetee
    while True:
        params = {"paginationOffset": offset, "paginationNumber": page_size}
        data = fleetee_api_get("/vehicles", params=params)
        if not data or "list" not in data:
            break
        vehicles.extend(data["list"])
        offset += page_size
        if offset >= data.get("total", 0):
            break
    return vehicles

if __name__ == "__main__":
    print("🔎 Récupération de tous les véhicules Fleetee...")
    vehicles = list_all_vehicles()
    print(f"Nombre total de véhicules récupérés : {len(vehicles)}")
    for v in vehicles:
        print(f"ID: {v.get('id')}, Plaque: {v.get('registration_number')}, Marque: {v.get('brand')}, Modèle: {v.get('model')}")