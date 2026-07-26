import requests
from decouple import config

FLEETEE_API_KEY = config('FLEETEE_API_KEY')
FLEETEE_SECRET_KEY = config('FLEETEE_SECRET_KEY')

url = "https://api.fleetee.io/bookings"
headers = {
    "x-api-key": FLEETEE_API_KEY,
    "secret-key": FLEETEE_SECRET_KEY,
    "Accept": "application/json"
}
response = requests.get(url, headers=headers)
print(response.status_code)
print(response.text)