import requests
import time

BASE_URL = "http://127.0.0.1:5000"
session = requests.Session()

# 1. Test Login
print("Testing Login...")
resp = session.post(f"{BASE_URL}/api/login", json={"username": "admin", "password": "admin"})
print(f"Login Response: {resp.status_code}")
try:
    print(f"Login Data: {resp.json()}")
except:
    print(f"Login Text: {resp.text}")

# 2. Test Attendance (Success)
print("\nTesting Attendance Success...")
resp = session.post(f"{BASE_URL}/api/attendance", json={"student_id": "ST001"})
print(f"Attendance Response: {resp.status_code}")
try:
    print(f"Attendance Data: {resp.json()}")
except:
    print(f"Attendance Text: {resp.text}")

# 3. Test Duplicate Scan (Failure)
print("\nTesting Duplicate Scan...")
resp = session.post(f"{BASE_URL}/api/attendance", json={"student_id": "ST001"})
print(f"Duplicate Response: {resp.status_code}, {resp.json()}")

# 4. Test Unauthorized (New Session)
print("\nTesting Unauthorized Access...")
new_session = requests.Session()
resp = new_session.post(f"{BASE_URL}/api/attendance", json={"student_id": "ST001"})
print(f"Unauthorized Response: {resp.status_code}")
