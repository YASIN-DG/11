import random
from locust import HttpUser, task, between, events

class AttendanceSystemUser(HttpUser):
    wait_time = between(1, 3)
    
    # 10 valid student IDs pre-populated from database for accurate testing
    valid_ids = ["101", "102", "103", "104", "105", "106", "107", "108", "109", "110"]
    credentials = {"username": "admin", "password": "securepassword"}
    
    def on_start(self):
        self.client.post("/api/login", json=self.credentials)

    @task(2)
    def lookup_student(self):
        student_id = random.choice(self.valid_ids)
        self.client.get(f"/api/students/lookup/{student_id}")

    @task(1)
    def register_attendance(self):
        student_id = random.choice(self.valid_ids)
        self.client.post("/api/attendance", json={"student_id": student_id})
