import sqlite3
conn = sqlite3.connect('attendance.db')
cursor = conn.cursor()
cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
tables = cursor.fetchall()
print("Tables in database:")
for table in tables:
    print(f"- {table[0]}")

# Check settings
cursor.execute("SELECT key, value FROM settings")
print("\nCurrent Settings:")
for row in cursor.fetchall():
    print(f"{row[0]}: {row[1]}")
conn.close()
