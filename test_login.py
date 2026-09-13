import requests
import json

# Try to login with default credentials
data = {"username": "1", "password": "123456"}
url = "http://127.0.0.1:5000/api/login"

try:
    response = requests.post(url, json=data)
    print(f"Status Code: {response.status_code}")
    print(f"Response Body: {response.text}")
except Exception as e:
    print(f"Error: {e}")
