import sys
import os
import threading
import time
import socket
import sqlite3
import secrets
import queue
import requests
import logging
import shutil
import io
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from functools import wraps

from PySide6.QtWidgets import QApplication, QMainWindow, QVBoxLayout, QWidget, QSystemTrayIcon, QMenu
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtCore import QUrl, Qt, QTimer
from PySide6.QtGui import QIcon, QAction
from PySide6.QtWebEngineCore import QWebEnginePage

from flask import Flask, request, jsonify, send_from_directory, send_file, session, redirect, url_for, abort
from dotenv import load_dotenv
from werkzeug.security import generate_password_hash, check_password_hash

load_dotenv()

# --- Flask Configuration ---
def resource_path(relative_path):
    """ Get absolute path to resource, works for dev and for PyInstaller """
    try:
        # PyInstaller creates a temp folder and stores path in _MEIPASS
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)

static_dir = resource_path('static')
db_path = resource_path("attendance.db")
CRASH_LOG_PATH = resource_path("crash_log.txt")

app = Flask(__name__, static_folder=static_dir)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'default_fallback_secret_key')

# --- Logging & Backup Setup ---
logging.basicConfig(level=logging.INFO)
handler = RotatingFileHandler('app.log', maxBytes=1000000, backupCount=3)
app.logger.addHandler(handler)

def log_crash(e):
    try:
        with open(CRASH_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {str(e)}\n")
    except:
        pass

# Notification Queue Helper
def get_db():
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.row_factory = sqlite3.Row
    return conn

def queue_notification(student_id, channel, notification_type, message):
    conn = get_db()
    conn.execute("""INSERT INTO notifications_log 
        (student_id, channel, notification_type, message, status, created_at) 
        VALUES (?, ?, ?, ?, 'pending', ?)""",
        (student_id, channel, notification_type, message, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def get_telegram_settings():
    conn = get_db()
    try:
        settings = {row['key']: row['value'] for row in conn.execute("SELECT * FROM settings WHERE key IN ('telegram_bot_token', 'telegram_chat_id')").fetchall()}
        return settings.get('telegram_bot_token'), settings.get('telegram_chat_id')
    finally:
        conn.close()

def notification_worker():
    import pywhatkit
    
    while True:
        token, chat_id = get_telegram_settings()
        
        conn = get_db()
        # Get oldest pending notification
        notif = conn.execute("""SELECT * FROM notifications_log 
                               WHERE status IN ('pending', 'failed') 
                               ORDER BY created_at ASC LIMIT 1""").fetchone()
        
        if not notif:
            conn.close()
            time.sleep(10) # Wait before next check
            continue
            
        print(f"DEBUG: Processing notification ID {notif['id']} for student {notif['student_id']}")
            
        student_id = notif['student_id']
        channel = notif['channel']
        message = notif['message']
        notif_id = notif['id']
        
        status = "failed"
        error_msg = ""
        
        try:
            if channel == 'telegram':
                if not token or not chat_id:
                    error_msg = "Telegram not configured"
                    print("DEBUG: Telegram not configured")
                else:
                    url = f"https://api.telegram.org/bot{token}/sendMessage"
                    print(f"DEBUG: Attempting to send to {url}")
                    resp = requests.post(url, json={"chat_id": chat_id, "text": message}, timeout=10)
                    if resp.status_code == 200:
                        status = "sent"
                        print("DEBUG: Sent successfully")
                    else:
                        error_msg = resp.text
                        print(f"DEBUG: API error: {error_msg}")
            elif channel == 'whatsapp':
                # Need phone number. Placeholder
                phone = "+1234567890" 
                pywhatkit.sendwhatmsg_instantly(phone, message, wait_time=15, tab_close=True)
                status = "sent"
            else:
                error_msg = "No channel"
        except Exception as e:
            error_msg = str(e)
            print(f"DEBUG: Exception: {error_msg}")
            
        # Update log
        if status == 'sent':
            conn.execute("UPDATE notifications_log SET status='sent', sent_at=?, error=NULL WHERE id=?", (datetime.now().isoformat(), notif_id))
        else:
            conn.execute("UPDATE notifications_log SET status='failed', error=?, retry_count=retry_count+1 WHERE id=?", (error_msg, notif_id))
        conn.commit()
        conn.close()

# Start workers
threading.Thread(target=notification_worker, daemon=True).start()

@app.get("/api/notifications/status")
@require_role('admin')
def get_notification_status():
    conn = get_db()
    try:
        count = conn.execute("SELECT COUNT(*) FROM notifications_log WHERE status IN ('pending', 'failed')").fetchone()[0]
        return jsonify({"pending_count": count})
    finally:
        conn.close()

def require_role(roles):
    if isinstance(roles, str):
        roles = [roles]
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if 'user_role' not in session or session['user_role'] not in roles:
                abort(403)
            return f(*args, **kwargs)
        return decorated_function
    return decorator

# --- Telegram API Endpoints ---
@app.post("/api/settings/telegram")
@require_role('admin')
def save_telegram_settings():
    data = request.get_json()
    token = data.get("token")
    chat_id = data.get("chat_id")
    conn = get_db()
    try:
        if token:
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('telegram_bot_token', ?)", (token.strip(),))
        if chat_id:
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('telegram_chat_id', ?)", (chat_id.strip(),))
        conn.commit()
        return jsonify({"success": True})
    finally:
        conn.close()

@app.post("/api/settings/telegram/test")
@require_role('admin')
def test_telegram_connection():
    token, chat_id = get_telegram_settings()
    if not token or not chat_id:
        return jsonify({"success": False, "message": "Telegram not configured"}), 400
    
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        resp = requests.post(url, json={"chat_id": chat_id, "text": "✅ Telegram test successful.\nAttendance System is connected correctly."}, timeout=10)
        if resp.status_code == 200:
            return jsonify({"success": True, "message": "Telegram connection successful."})
        else:
            return jsonify({"success": False, "message": f"Telegram API error: {resp.text}"}), 400
    except Exception as e:
        return jsonify({"success": False, "message": f"Connection failed: {str(e)}"}), 500


def backup_worker():
    while True:
        try:
            os.makedirs("backups", exist_ok=True)
            ts = datetime.now().strftime('%Y-%m-%d')
            dest = f"backups/attendance_{ts}.db"
            
            # Safe SQLite Backup
            src_conn = sqlite3.connect(db_path)
            dest_conn = sqlite3.connect(dest)
            src_conn.backup(dest_conn)
            dest_conn.close()
            src_conn.close()
            
            # Retention
            backups = sorted([f for f in os.listdir("backups") if f.startswith("attendance_")])
            for b in backups[:-7]:
                os.remove(f"backups/{b}")
        except Exception as e:
            app.logger.error(f"Backup failed: {e}")
        time.sleep(86400) # 24h

threading.Thread(target=backup_worker, daemon=True).start()

# --- Database Setup ---
def create_tables():
    conn = get_db()
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("""CREATE TABLE IF NOT EXISTS students (
            student_id TEXT PRIMARY KEY, 
            name TEXT, 
            level TEXT, 
            group_name TEXT, 
            parent_name TEXT, 
            phone TEXT, 
            active_status INTEGER DEFAULT 1, 
            payment_status TEXT DEFAULT 'unpaid'
        )""")
        # Add new columns safely
        columns = [row[1] for row in conn.execute("PRAGMA table_info(students)").fetchall()]
        if 'amount_due' not in columns:
            conn.execute("ALTER TABLE students ADD COLUMN amount_due REAL DEFAULT 0")
        if 'last_due_date' not in columns:
            conn.execute("ALTER TABLE students ADD COLUMN last_due_date TEXT")
            
        conn.execute("""CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT,
            category TEXT,
            amount REAL,
            date TEXT,
            notes TEXT
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT, 
            student_id TEXT, 
            date TEXT, 
            time TEXT, 
            timestamp DATETIME,
            status TEXT DEFAULT 'present',
            FOREIGN KEY(student_id) REFERENCES students(student_id)
        )""")
        conn.execute("CREATE TABLE IF NOT EXISTS subjects (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT)")
        conn.execute("""CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE,
            password_hash TEXT,
            role TEXT
        )""")
        # Recreate table with full fields
        conn.execute("DROP TABLE IF EXISTS notifications_log")
        conn.execute("""CREATE TABLE IF NOT EXISTS notifications_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id TEXT,
            channel TEXT,
            notification_type TEXT,
            message TEXT,
            status TEXT DEFAULT 'pending',
            error TEXT,
            retry_count INTEGER DEFAULT 0,
            created_at DATETIME,
            sent_at DATETIME,
            FOREIGN KEY(student_id) REFERENCES students(student_id)
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            day TEXT,
            time TEXT,
            max_capacity INTEGER,
            teacher_id INTEGER,
            FOREIGN KEY(teacher_id) REFERENCES users(id)
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS student_groups (
            group_id INTEGER,
            student_id TEXT,
            PRIMARY KEY(group_id, student_id),
            FOREIGN KEY(group_id) REFERENCES groups(id),
            FOREIGN KEY(student_id) REFERENCES students(student_id)
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS grades (
            id INTEGER PRIMARY KEY AUTOINCREMENT, 
            student_id TEXT, 
            subject_id INTEGER, 
            exam_name TEXT, 
            score REAL, 
            max_score REAL, 
            date TEXT, 
            FOREIGN KEY(student_id) REFERENCES students(student_id), 
            FOREIGN KEY(subject_id) REFERENCES subjects(id)
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS extra_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id TEXT,
            group_id INTEGER,
            type TEXT,
            subject TEXT,
            date TEXT,
            time TEXT,
            status TEXT DEFAULT 'pending',
            created_by_teacher_id INTEGER,
            notified_at DATETIME,
            created_at DATETIME,
            FOREIGN KEY(student_id) REFERENCES students(student_id),
            FOREIGN KEY(group_id) REFERENCES groups(id),
            FOREIGN KEY(created_by_teacher_id) REFERENCES users(id)
        )""")
        
        # Add Indexes
        conn.execute("CREATE INDEX IF NOT EXISTS idx_students_code ON students(student_id);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_attendance_student_date ON attendance(student_id, date);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_attendance_date_status ON attendance(date, status);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_grades_student ON grades(student_id);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_notifications_status ON notifications_log(status);")
        
        check = conn.execute("SELECT * FROM settings WHERE key='is_setup'").fetchone()
        if not check:
            conn.execute("INSERT INTO settings (key, value) VALUES ('is_setup', '0')")
            
            # Ensure default admin '1' exists with hashed password '123456'
            admin_username = '1'
            admin_password = '123456'
            admin_pw_hash = generate_password_hash(admin_password)
            
            # Check if admin already exists
            existing_admin = conn.execute("SELECT * FROM users WHERE username=?", (admin_username,)).fetchone()
            if not existing_admin:
                conn.execute("INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)", (admin_username, admin_pw_hash, "admin"))
                print(f"Default admin user created: {admin_username} / {admin_password}")
            else:
                print("Default admin user already exists.")
        conn.commit()
    finally:
        conn.close()

# --- Routes ---
@app.route('/')
def serve_index():
    return send_from_directory(static_dir, 'index.html')

@app.post("/api/login")
def login():
    data = request.get_json()
    username = data.get("username")
    password = data.get("password")
    conn = get_db()
    try:
        user = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if user and check_password_hash(user['password_hash'], password):
            session['user_id'] = user['id']
            session['user_role'] = user['role']
            return jsonify({"success": True, "role": user['role']})
        return jsonify({"success": False, "message": "Invalid credentials"}), 401
    finally:
        conn.close()

@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify({"success": True})

@app.get("/<path:filename>")
def serve_static(filename):
    return send_from_directory(static_dir, filename)

@app.get("/api/config")
def get_config():
    conn = get_db()
    try:
        # Only expose safe, cosmetic fields
        safe_keys = ['center_name', 'manager_name', 'phone', 'address', 'is_setup']
        settings = {row['key']: row['value'] for row in conn.execute("SELECT * FROM settings WHERE key IN ({})".format(','.join(['?']*len(safe_keys))), safe_keys).fetchall()}
        return jsonify(settings)
    finally:
        conn.close()

@app.get("/api/settings")
@require_role('admin')
def get_settings():
    conn = get_db()
    try:
        settings = {row['key']: row['value'] for row in conn.execute("SELECT * FROM settings").fetchall()}
        return jsonify(settings)
    finally:
        conn.close()

@app.post("/api/setup")
def setup():
    data = request.get_json()
    conn = get_db()
    try:
        # Extract and remove admin credentials
        admin_username = data.pop('admin_username', None)
        admin_password = data.pop('admin_password', None)
        data.pop('admin_password_confirm', None) # remove confirm too
        
        if admin_username and admin_password:
             admin_pw_hash = generate_password_hash(admin_password)
             conn.execute("INSERT OR REPLACE INTO users (username, password_hash, role) VALUES (?, ?, ?)", (admin_username, admin_pw_hash, "admin"))

        for key, value in data.items():
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('is_setup', '1')")
        conn.commit()
        return jsonify({"success": True})
    finally:
        conn.close()

@app.get("/api/dashboard")
def get_dashboard():
    conn = get_db()
    try:
        total = conn.execute("SELECT COUNT(*) FROM students").fetchone()[0]
        today = datetime.now().strftime('%Y-%m-%d')
        present = conn.execute("SELECT COUNT(DISTINCT student_id) FROM attendance WHERE date=? AND status='present'", (today,)).fetchone()[0]
        absent = conn.execute("SELECT COUNT(DISTINCT student_id) FROM attendance WHERE date=? AND status='absent'", (today,)).fetchone()[0]
        return jsonify({"total_students": total, "present_today": present, "absent_today": absent, "recent_attendance": []})
    finally:
        conn.close()

@app.get("/api/stats/chart")
def get_chart_stats():
    return jsonify([{"date": "اليوم", "count": 0}])

@app.post("/api/attendance")
@require_role(['receptionist', 'admin'])
def register_attendance():
    data = request.get_json()
    sid = data.get("student_id")
    status = data.get("status", "present") # Default to present
    now = datetime.now()
    today = now.strftime('%Y-%m-%d')
    conn = get_db()
    try:
        student = conn.execute("SELECT name, payment_status FROM students WHERE student_id=?", (sid,)).fetchone()
        if not student:
            return jsonify({"success": False, "message": "الطالب غير موجود"}), 404
        
        # Duplicate guard
        if status == 'present':
            last_scan = conn.execute("SELECT timestamp FROM attendance WHERE student_id=? AND status='present' ORDER BY timestamp DESC LIMIT 1", (sid,)).fetchone()
            if last_scan:
                last_time = datetime.fromisoformat(last_scan['timestamp'])
                if now - last_time < timedelta(seconds=30):
                    return jsonify({"success": False, "message": "تم تسجيل الحضور بالفعل مؤخراً"}), 400
        else:
            # Already absent today?
            existing = conn.execute("SELECT id FROM attendance WHERE student_id=? AND date=? AND status='absent'", (sid, today)).fetchone()
            if existing:
                return jsonify({"success": False, "message": "تم تسجيل الغياب بالفعل اليوم"}), 400

        conn.execute("INSERT INTO attendance (student_id, date, time, timestamp, status) VALUES (?, ?, ?, ?, ?)", 
                     (sid, today, now.strftime('%H:%M'), now.isoformat(), status))
        conn.commit()
        
        if status == 'present':
            # Queue notifications
            queue_notification(sid, 'telegram', 'attendance', f"الطالب {student['name']} حضر اليوم.")
            queue_notification(sid, 'whatsapp', 'attendance', f"الطالب {student['name']} حضر اليوم.")
            return jsonify({"success": True, "studentName": student['name'], "payment_status": student['payment_status']})
        else:
            # Duplicate notification guard (for today)
            recent_notif = conn.execute("""SELECT id FROM notifications_log 
                                           WHERE student_id=? AND notification_type='absent' 
                                           AND created_at LIKE ?""", (sid, f"{today}%")).fetchone()
            if not recent_notif:
                queue_notification(sid, 'telegram', 'absent', f"الطالب {student['name']} غاب اليوم، وفيه حصة تعويض متاحة، برجاء التواصل مع المستر لتحديد الميعاد.")
            return jsonify({"success": True})
    finally:
        conn.close()

@app.get("/api/students")
def list_students():
    page = int(request.args.get('page', 1))
    limit = int(request.args.get('limit', 50))
    offset = (page - 1) * limit
    conn = get_db()
    try:
        total = conn.execute("SELECT COUNT(*) FROM students").fetchone()[0]
        rows = conn.execute("SELECT * FROM students LIMIT ? OFFSET ?", (limit, offset)).fetchall()
        return jsonify({"students": [dict(r) for r in rows], "total": total, "page": page, "limit": limit})
    finally:
        conn.close()

@app.post("/api/students")
def add_student():
    data = request.get_json()
    conn = get_db()
    try:
        conn.execute("INSERT INTO students (student_id, name, level, group_name, parent_name, phone) VALUES (?,?,?,?,?,?)",
                     (data['student_id'], data['name'], data['level'], data['group_name'], data['parent_name'], data['phone']))
        conn.commit()
        return jsonify({"success": True})
    finally:
        conn.close()

@app.put("/api/students/<sid>")
def update_student(sid):
    data = request.get_json()
    conn = get_db()
    try:
        conn.execute("UPDATE students SET name=?, level=?, group_name=?, parent_name=?, phone=? WHERE student_id=?",
                     (data['name'], data['level'], data['group_name'], data['parent_name'], data['phone'], sid))
        conn.commit()
        return jsonify({"success": True})
    finally:
        conn.close()

@app.delete("/api/students/<sid>")
def delete_student(sid):
    conn = get_db()
    try:
        conn.execute("DELETE FROM students WHERE student_id=?", (sid,))
        conn.commit()
        return jsonify({"success": True})
    finally:
        conn.close()

@app.get("/api/subjects")
def get_subjects():
    conn = get_db()
    try:
        rows = conn.execute("SELECT * FROM subjects").fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()

@app.post("/api/subjects")
def add_subject():
    name = request.get_json().get("name")
    conn = get_db()
    try:
        conn.execute("INSERT INTO subjects (name) VALUES (?)", (name,))
        conn.commit()
        return jsonify({"success": True})
    finally:
        conn.close()

@app.get("/api/grades/<sid>")
def get_grades(sid):
    conn = get_db()
    try:
        rows = conn.execute("SELECT g.*, s.name as subject_name FROM grades g JOIN subjects s ON g.subject_id=s.id WHERE g.student_id=?", (sid,)).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()

@app.post("/api/grades")
def add_grade():
    data = request.get_json()
    conn = get_db()
    try:
        conn.execute("INSERT INTO grades (student_id, subject_id, exam_name, score, max_score, date) VALUES (?,?,?,?,?,?)",
                     (data['student_id'], data['subject_id'], data['exam_name'], data['score'], data['max_score'], datetime.now().strftime('%Y-%m-%d')))
        conn.commit()
        return jsonify({"success": True})
    finally:
        conn.close()

@app.get("/api/finances")
def get_finances():
    conn = get_db()
    try:
        rows = conn.execute("SELECT * FROM transactions").fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()

@app.post("/api/transactions")
@require_role('admin')
def add_transaction():
    data = request.get_json()
    conn = get_db()
    try:
        conn.execute("INSERT INTO transactions (type, category, amount, date) VALUES (?,?,?,?)",
                     (data['type'], data['category'], data['amount'], datetime.now().strftime('%Y-%m-%d')))
        conn.commit()
        return jsonify({"success": True})
    finally:
        conn.close()

@app.get("/api/finances/stats")
@require_role('admin')
def get_finance_stats():
    conn = get_db()
    try:
        rev = conn.execute("SELECT SUM(amount) FROM transactions WHERE type='income'").fetchone()[0] or 0
        exp = conn.execute("SELECT SUM(amount) FROM transactions WHERE type='expense'").fetchone()[0] or 0
        return jsonify({"revenue": rev, "expenses": exp, "net_profit": rev - exp})
    finally:
        conn.close()

@app.get("/api/reports/pdf/<student_id>")
@require_role('admin')
def get_pdf_report(student_id):
    month = request.args.get('month', datetime.now().strftime('%Y-%m'))
    conn = get_db()
    student = conn.execute("SELECT * FROM students WHERE student_id=?", (student_id,)).fetchone()
    if not student:
        conn.close()
        return jsonify({"message": "Student not found"}), 404
        
    attendance = conn.execute("SELECT COUNT(*) FROM attendance WHERE student_id=? AND date LIKE ?", (student_id, f"{month}%")).fetchone()[0]
    grades = conn.execute("SELECT AVG(score) FROM grades WHERE student_id=? AND date LIKE ?", (student_id, f"{month}%")).fetchone()[0] or 0
    conn.close()

    from reportlab.pdfgen import canvas
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer)
    c.drawString(100, 750, f"Student: {student['name']}")
    c.drawString(100, 730, f"ID: {student['student_id']}")
    c.drawString(100, 710, f"Month: {month}")
    c.drawString(100, 690, f"Attendance Count: {attendance}")
    c.drawString(100, 670, f"Grade Average: {grades:.2f}")
    c.drawString(100, 650, f"Payment Status: {student['payment_status']}")
    c.save()
    buffer.seek(0)
    return send_file(buffer, mimetype='application/pdf', download_name=f"report_{student_id}.pdf")

# --- Reports API ---
@app.get("/api/reports/group/<gid>/pdf")
@require_role(['teacher', 'admin'])
def group_report_pdf(gid):
    # Implementation placeholder for PDF generation
    return jsonify({"success": True, "message": "PDF placeholder"})

@app.get("/api/reports/group/<gid>/excel")
@require_role(['teacher', 'admin'])
def group_report_excel(gid):
    # Implementation placeholder for Excel generation
    return jsonify({"success": True, "message": "Excel placeholder"})

@app.get("/api/reports/monthly/pdf")
@require_role('admin')
def monthly_report_pdf():
    month = request.args.get('month', datetime.now().strftime('%Y-%m'))
    # Implementation placeholder
    return jsonify({"success": True, "message": "Monthly PDF placeholder"})

@app.get("/api/reports/monthly/excel")
@require_role('admin')
def monthly_report_excel():
    month = request.args.get('month', datetime.now().strftime('%Y-%m'))
    # Implementation placeholder
    return jsonify({"success": True, "message": "Monthly Excel placeholder"})

# --- Finance API ---
@app.get("/api/expenses")
@require_role('admin')
def get_expenses():
    conn = get_db()
    try:
        rows = conn.execute("SELECT * FROM transactions WHERE type='expense'").fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()

@app.post("/api/expenses")
@require_role('admin')
def add_expense():
    data = request.get_json()
    conn = get_db()
    try:
        conn.execute("INSERT INTO transactions (type, category, amount, date, notes) VALUES ('expense', ?, ?, ?, ?)",
                     (data['category'], data['amount'], datetime.now().strftime('%Y-%m-%d'), data.get('notes', '')))
        conn.commit()
        return jsonify({"success": True})
    finally:
        conn.close()

@app.get("/api/finances/balances")
@require_role('admin')
def get_outstanding_balances():
    conn = get_db()
    try:
        rows = conn.execute("SELECT student_id, name, amount_due, last_due_date FROM students WHERE amount_due > 0").fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()

@app.post("/api/extra-sessions")
@require_role(['teacher', 'admin'])
def schedule_extra_session():
    data = request.get_json()
    student_id = data.get("student_id")
    group_id = data.get("group_id")
    stype = data.get("type")
    subject = data.get("subject")
    date = data.get("date")
    time = data.get("time")
    teacher_id = session.get('user_id')
    
    conn = get_db()
    try:
        conn.execute("INSERT INTO extra_sessions (student_id, group_id, type, subject, date, time, created_by_teacher_id, created_at) VALUES (?,?,?,?,?,?,?,?)",
                     (student_id, group_id, stype, subject, date, time, teacher_id, datetime.now().isoformat()))
        conn.commit()
        
        # Notify
        if group_id:
            # Get all students in group
            students = conn.execute("SELECT s.name, s.telegram_chat_id FROM students s JOIN student_groups sg ON s.student_id=sg.student_id WHERE sg.group_id=?", (group_id,)).fetchall()
            for s in students:
                if s['telegram_chat_id']:
                    queue_notification(None, 'telegram', 'extra_session', f"حصة {stype} جديدة: {subject} يوم {date} الساعة {time}")
        elif student_id:
            s = conn.execute("SELECT name, telegram_chat_id FROM students WHERE student_id=?", (student_id,)).fetchone()
            if s and s['telegram_chat_id']:
                queue_notification(student_id, 'telegram', 'extra_session', f"حصة {stype} جديدة: {subject} يوم {date} الساعة {time}")
                
        return jsonify({"success": True})
    finally:
        conn.close()

@app.post("/api/groups/<gid>/students")
@require_role(['admin', 'teacher'])
def add_student_to_group(gid):
    data = request.get_json()
    sid = data['student_id']
    conn = get_db()
    try:
        # Check capacity
        group = conn.execute("SELECT max_capacity FROM groups WHERE id=?", (gid,)).fetchone()
        count = conn.execute("SELECT COUNT(*) FROM student_groups WHERE group_id=?", (gid,)).fetchone()[0]
        if count >= group['max_capacity']:
            return jsonify({"success": False, "message": "Group is full"}), 400

        conn.execute("INSERT INTO student_groups (group_id, student_id) VALUES (?, ?)", (gid, sid))
        conn.commit()
        return jsonify({"success": True})
    finally:
        conn.close()

# --- PyQt Logic ---
PORT = 5000
URL = f"http://127.0.0.1:{PORT}"

def is_server_ready():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(('127.0.0.1', PORT)) == 0

class WebPage(QWebEnginePage):
    def featurePermissionRequested(self, url, feature):
        if feature in (QWebEnginePage.MediaAudioVideoCapture, QWebEnginePage.MediaVideoCapture, QWebEnginePage.MediaAudioCapture):
            self.setFeaturePermission(url, feature, QWebEnginePage.PermissionGrantedByUser)
        else:
            super().featurePermissionRequested(url, feature)

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("نظام الحضور الذكي")
        self.resize(1200, 800)
        
        # Center the window
        screen = QApplication.primaryScreen().geometry()
        size = self.geometry()
        self.move((screen.width() - size.width()) // 2, (screen.height() - size.height()) // 2)

        # Web View
        self.browser = QWebEngineView()
        self.page = WebPage(self.browser)
        self.browser.setPage(self.page)
        self.setCentralWidget(self.browser)
        
        # Wait for server and then load
        self.check_server_timer = QTimer(self)
        self.check_server_timer.timeout.connect(self.check_server_and_load)
        self.check_server_timer.start(500)
        
        # Tray Icon
        self.setup_tray()

    def check_server_and_load(self):
        if is_server_ready():
            self.check_server_timer.stop()
            print(f"SERVER READY: {URL}")
            self.browser.setUrl(QUrl(URL))
        else:
            print("SERVER STARTING...")

    def setup_tray(self):
        self.tray_icon = QSystemTrayIcon(self)
        self.tray_icon.setIcon(QIcon.fromTheme("view-refresh"))
        
        show_action = QAction("فتح النظام", self)
        quit_action = QAction("إغلاق نهائي", self)
        
        show_action.triggered.connect(self.showNormal)
        quit_action.triggered.connect(QApplication.instance().quit)
        
        tray_menu = QMenu()
        tray_menu.addAction(show_action)
        tray_menu.addSeparator()
        tray_menu.addAction(quit_action)
        
        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.show()

    def closeEvent(self, event):
        # Minimize to tray instead of closing
        if self.tray_icon.isVisible():
            self.hide()
            self.tray_icon.showMessage(
                "نظام الحضور",
                "التطبيق ما زال يعمل في الخلفية لتلقي التنبيهات.",
                QSystemTrayIcon.Information,
                2000
            )
            event.ignore()
        else:
            event.accept()

def run_flask():
    create_tables()
    from waitress import serve
    print(f"🚀 System is running on http://0.0.0.0:{PORT}")
    serve(app, host='0.0.0.0', port=PORT, threads=8)

if __name__ == "__main__":
    try:
        # Start Flask in a separate thread
        flask_thread = threading.Thread(target=run_flask, daemon=True)
        flask_thread.start()
        
        # Start PyQt App
        qt_app = QApplication(sys.argv)
        qt_app.setApplicationName("SmartAttendance")
        qt_app.setQuitOnLastWindowClosed(False)
        
        window = MainWindow()
        window.show()
        
        sys.exit(qt_app.exec())
    except Exception as e:
        log_crash(e)
        raise
