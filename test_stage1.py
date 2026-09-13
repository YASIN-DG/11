import requests
import time
import sqlite3

BASE_URL = "http://127.0.0.1:5000"
session = requests.Session()

# Login
session.post(f"{BASE_URL}/api/login", json={"username": "rec", "password": "..."})

# Tests
print("A/B/C: Testing Attendance and Queuing...")
resp = session.post(f"{BASE_URL}/api/attendance", json={"student_id": "ST001"})
print(f"Attendance Response: {resp.status_code}")

# Check DB
conn = sqlite3.connect('attendance.db')
notifs = conn.execute("SELECT * FROM notifications_log").fetchall()
print(f"Notifications in DB: {len(notifs)}")
for n in notifs:
    print(f"  - {n[2]} ({n[4]}): {n[5]}")
conn.close()
