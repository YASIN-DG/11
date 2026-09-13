from werkzeug.security import generate_password_hash
import sqlite3
import os

password = 'admin'
hash_val = generate_password_hash(password)
print(f"BEFORE: repr={repr(hash_val)}, len={len(hash_val)}")

# Use app's get_db logic directly to ensure environment consistency
db_path = "attendance.db"
conn = sqlite3.connect(db_path)
conn.execute('INSERT OR REPLACE INTO users (username, password_hash, role) VALUES (?, ?, ?)', ('admin', hash_val, 'admin'))
conn.commit()

row = conn.execute('SELECT password_hash FROM users WHERE username="admin"').fetchone()
stored_hash = row[0]
conn.close()

print(f"AFTER: repr={repr(stored_hash)}, len={len(stored_hash)}")
if hash_val == stored_hash:
    print("SUCCESS: Hash matched.")
else:
    print("FAILURE: Hash did not match.")
