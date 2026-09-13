import sqlite3
conn = sqlite3.connect('attendance.db')
val = conn.execute("SELECT value FROM settings WHERE key='is_setup'").fetchone()[0]
print(f"is_setup={val}")
conn.close()
