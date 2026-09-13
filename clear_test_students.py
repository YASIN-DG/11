import sqlite3

def clear_test_data():
    confirm = input("Are you sure you want to delete all student-related data? (type 'yes'): ")
    if confirm.lower() != 'yes':
        print("Operation cancelled.")
        return

    try:
        conn = sqlite3.connect('attendance.db')
        conn.execute("PRAGMA foreign_keys = ON")
        cursor = conn.cursor()

        # Tables to clear in order to respect FK constraints
        tables = ['grades', 'student_groups', 'notifications_log', 'attendance', 'students']
        
        for table in tables:
            cursor.execute(f"DELETE FROM {table}")
            print(f"Deleted {cursor.rowcount} rows from {table}")

        conn.commit()
        conn.close()
        print("All student-related data deleted successfully.")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    clear_test_data()
