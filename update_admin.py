import sqlite3
import os
from werkzeug.security import generate_password_hash

def manage_admin_user(db_path="attendance.db"):
    if not os.path.exists(db_path):
        print(f"Error: Database file '{db_path}' not found.")
        return

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    try:
        # Check if users table exists
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='users';")
        if not cursor.fetchone():
            print("Error: 'users' table not found.")
            return

        # Prepare new credentials
        new_username = "1"
        new_password = "123456"
        hashed_password = generate_password_hash(new_password)

        # Try to find an existing user with role 'admin'
        cursor.execute("SELECT id FROM users WHERE role='admin' LIMIT 1")
        existing_admin = cursor.fetchone()

        if existing_admin:
            # Update existing admin
            admin_id = existing_admin[0]
            cursor.execute(
                "UPDATE users SET username=?, password_hash=? WHERE id=?",
                (new_username, hashed_password, admin_id)
            )
            print(f"Admin user updated successfully. Username: {new_username}, Password: {new_password}")
        else:
            # Add new admin user
            cursor.execute(
                "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
                (new_username, hashed_password, "admin")
            )
            print(f"New admin user created successfully. Username: {new_username}, Password: {new_password}")

        conn.commit()

    except sqlite3.Error as e:
        print(f"An error occurred: {e}")
    finally:
        conn.close()

if __name__ == "__main__":
    manage_admin_user()
