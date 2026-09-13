import os
import sys
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
import multiprocessing
import re
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

import arabic_reshaper
from bidi.algorithm import get_display
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

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

# --- Arabic PDF font registration (for correct shaping/RTL in reportlab) ---
ARABIC_FONT_PATH = resource_path("fonts/NotoNaskhArabic-Regular.ttf")
try:
    pdfmetrics.registerFont(TTFont('Arabic', ARABIC_FONT_PATH))
    ARABIC_FONT_LOADED = True
except Exception:
    ARABIC_FONT_LOADED = False

def ar(text):
    """Reshape + reorder Arabic text so ReportLab renders it correctly (RTL)."""
    reshaped = arabic_reshaper.reshape(str(text))
    return get_display(reshaped)

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

# --- Global JSON Error Handling ---
# يضمن أن أي خطأ في أي مسار /api/* يرجع JSON دايماً بدل صفحة HTML افتراضية
from werkzeug.exceptions import HTTPException

@app.errorhandler(Exception)
def handle_any_exception(e):
    if isinstance(e, HTTPException):
        return jsonify({
            "success": False,
            "message": e.description or "حدث خطأ"
        }), e.code

    log_crash(e)
    app.logger.error(f"Unhandled exception on {request.path}: {e}")
    return jsonify({
        "success": False,
        "message": "حدث خطأ غير متوقع في السيرفر"
    }), 500

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
        settings = {row['key']: row['value'] for row in conn.execute("SELECT * FROM settings WHERE key IN ('telegram_bot_token','telegram_chat_id','telegram_enabled','openai_api_key','openai_model')").fetchall()}
        return settings
    finally:
        conn.close()


def send_telegram_message(chat_id, message):
    settings = get_telegram_settings()
    token = settings.get('telegram_bot_token')
    if not token or not chat_id:
        return False, 'Telegram غير مُكوّن أو ولي الأمر غير مرتبط'
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        resp = requests.post(url, json={'chat_id': str(chat_id), 'text': message}, timeout=10)
        return (True, '') if resp.ok else (False, resp.text)
    except Exception as e:
        return False, str(e)


# =========================================================
# NON-BLOCKING WHATSAPP SENDER
# =========================================================
def _normalize_whatsapp_phone(phone):
    """Convert common Egyptian phone formats to international +20 format."""
    if not phone:
        return None

    raw = str(phone).strip()
    digits = ''.join(ch for ch in raw if ch.isdigit())

    if not digits:
        return None

    if digits.startswith('00'):
        digits = digits[2:]

    if digits.startswith('20'):
        return '+' + digits

    if digits.startswith('0') and len(digits) == 11:
        return '+20' + digits[1:]

    if len(digits) == 10 and digits.startswith('1'):
        return '+20' + digits

    return '+' + digits


def _whatsapp_process(phone, message):
    """Runs in a separate process so pywhatkit can never block Flask."""
    import pywhatkit

    pywhatkit.sendwhatmsg_instantly(
        phone,
        message,
        wait_time=10,
        tab_close=True
    )


def send_whatsapp_non_blocking(phone, message, max_seconds=25):
    """Try WhatsApp in an isolated process with a hard time limit."""
    normalized = _normalize_whatsapp_phone(phone)
    if not normalized:
        return False, 'رقم WhatsApp غير صحيح أو غير موجود'

    import multiprocessing

    process = multiprocessing.Process(
        target=_whatsapp_process,
        args=(normalized, message),
        name='WhatsAppSender'
    )
    process.start()
    process.join(max_seconds)

    if process.is_alive():
        app.logger.warning(
            f'WhatsApp sender timeout after {max_seconds}s for {normalized}'
        )
        process.terminate()
        process.join(3)
        return False, 'انتهت مهلة إرسال WhatsApp'

    if process.exitcode == 0:
        return True, ''

    return False, f'فشل إرسال WhatsApp (exit code: {process.exitcode})'


def _get_next_notification(channel):
    """Get one pending notification for a specific channel."""
    conn = get_db()
    try:
        return conn.execute("""
            SELECT * FROM notifications_log
            WHERE channel=?
              AND status IN ('pending','failed')
              AND COALESCE(retry_count,0) < 3
            ORDER BY created_at ASC
            LIMIT 1
        """, (channel,)).fetchone()
    finally:
        conn.close()


def _finish_notification(notif_id, status, error_msg=''):
    conn = get_db()
    try:
        if status == 'sent':
            conn.execute("""
                UPDATE notifications_log
                SET status='sent', sent_at=?, error=NULL
                WHERE id=?
            """, (datetime.now().isoformat(), notif_id))
        else:
            conn.execute("""
                UPDATE notifications_log
                SET status='failed',
                    error=?,
                    retry_count=COALESCE(retry_count,0)+1
                WHERE id=?
            """, (error_msg or 'Unknown error', notif_id))
        conn.commit()
    finally:
        conn.close()


def telegram_notification_worker():
    """Send queued Telegram notifications independently of WhatsApp."""
    while True:
        try:
            notif = _get_next_notification('telegram')
            if not notif:
                time.sleep(2)
                continue

            conn = get_db()
            try:
                student = conn.execute(
                    'SELECT telegram_chat_id FROM students WHERE student_id=?',
                    (notif['student_id'],)
                ).fetchone()
            finally:
                conn.close()

            chat_id = student['telegram_chat_id'] if student else None
            if not chat_id:
                _finish_notification(
                    notif['id'],
                    'failed',
                    'ولي الأمر غير مرتبط بـ Telegram'
                )
                time.sleep(1)
                continue

            ok, err = send_telegram_message(chat_id, notif['message'])
            _finish_notification(
                notif['id'],
                'sent' if ok else 'failed',
                err
            )

        except Exception as e:
            app.logger.error(f'Telegram notification worker error: {e}')
            time.sleep(3)


def whatsapp_notification_worker():
    """Send queued WhatsApp notifications without blocking the main app."""
    while True:
        try:
            notif = _get_next_notification('whatsapp')
            if not notif:
                time.sleep(2)
                continue

            conn = get_db()
            try:
                student = conn.execute(
                    'SELECT phone FROM students WHERE student_id=?',
                    (notif['student_id'],)
                ).fetchone()
            finally:
                conn.close()

            phone = student['phone'] if student else None

            if not phone:
                _finish_notification(
                    notif['id'],
                    'failed',
                    'رقم ولي الأمر غير موجود'
                )
                time.sleep(1)
                continue

            ok, err = send_whatsapp_non_blocking(
                phone,
                notif['message'],
                max_seconds=25
            )

            _finish_notification(
                notif['id'],
                'sent' if ok else 'failed',
                err
            )

            # Avoid hammering WhatsApp Web after a failed attempt.
            time.sleep(1)

        except Exception as e:
            app.logger.error(f'WhatsApp notification worker error: {e}')
            time.sleep(3)


# Backward-compatible name used by older code.
def notification_worker():
    telegram_notification_worker()


def handle_telegram_message(chat_id, text):
    conn = get_db()
    try:
        student = conn.execute('SELECT * FROM students WHERE telegram_chat_id=? LIMIT 1', (str(chat_id),)).fetchone()
        low = text.lower()
        if low in ('/start','start','ابدأ'):
            reply = (f"👋 أهلاً بك\n\nتم ربط الحساب بالطالب: {student['name']}\n\nاكتب /مساعدة للخدمات." if student else '👋 أهلاً بك\n\nأرسل كود الطالب لربط حساب ولي الأمر.')
        elif not student:
            target = conn.execute(
                'SELECT * FROM students WHERE student_id=? LIMIT 1',
                (text.upper(),)
            ).fetchone()

            if target:
                if target['telegram_chat_id'] and str(target['telegram_chat_id']) != str(chat_id):
                    reply = '⚠️ الطالب مرتبط بالفعل بحساب Telegram آخر. تواصل مع الإدارة.'
                else:
                    conn.execute(
                        'UPDATE students SET telegram_chat_id=? WHERE student_id=?',
                        (str(chat_id), target['student_id'])
                    )
                    conn.commit()
                    reply = (
                        f"✅ تم الربط بنجاح\n\n"
                        f"👨‍🎓 الطالب: {target['name']}\n"
                        f"🆔 الكود: {target['student_id']}\n\n"
                        "سيتم إرسال إشعارات الحضور والغياب لهذا الحساب تلقائياً.\n\n"
                        "اكتب /مساعدة."
                    )
            else:
                reply = (
                    "👋 أهلاً بك في مساعد المركز الذكي.\n\n"
                    "أنا جاهز للرد على رسائلك ومساعدتك.\n\n"
                    "📌 للحصول على بيانات طالب محدد، أرسل كود الطالب.\n"
                    "📋 /حضور — حالة الحضور اليوم\n"
                    "📊 /الشهر — تقرير الشهر\n"
                    "👨‍🎓 /بياناتي — بيانات الطالب\n"
                    "❓ /مساعدة — الخدمات المتاحة"
                )
        elif low in ('/مساعدة','/help','مساعدة'):
            reply = '🤖 مساعد المركز\n\n📋 /حضور — حالة اليوم\n📊 /الشهر — تقرير الشهر\n👨‍🎓 /بياناتي — بيانات الطالب\n❓ /مساعدة — المساعدة'
        elif low in ('/حضور','حضور'):
            today = datetime.now().strftime('%Y-%m-%d')
            a = conn.execute('SELECT * FROM attendance WHERE student_id=? AND date=? ORDER BY time DESC LIMIT 1', (student['student_id'],today)).fetchone()
            reply = (f"📋 حضور اليوم\n\n👨‍🎓 {student['name']}\n🟢 {a['status']}\n🕐 {a['time']}" if a else f"📋 حضور اليوم\n\n👨‍🎓 {student['name']}\n🔴 لم يتم تسجيل الحضور حتى الآن.")
        elif low in ('/الشهر','الشهر'):
            month = datetime.now().strftime('%Y-%m')
            rows = conn.execute('SELECT status FROM attendance WHERE student_id=? AND date LIKE ?', (student['student_id'],month+'%')).fetchall()
            present = sum(1 for r in rows if r['status']=='حاضر')
            reply = f"📊 تقرير الشهر\n\n👨‍🎓 {student['name']}\n🟢 مرات الحضور: {present}\n📌 إجمالي السجلات: {len(rows)}"
        elif low in ('/بياناتي','بياناتي'):
            reply = f"👨‍🎓 بيانات الطالب\n\nالاسم: {student['name']}\nالكود: {student['student_id']}\nالمستوى: {student['level'] or 'غير محدد'}\nالمجموعة: {student['group_name'] or 'غير محددة'}"
        else:
            reply = f"أهلاً بك 👋\nحسابك مرتبط بالطالب {student['name']}.\nاكتب /مساعدة لرؤية الخدمات."
        send_telegram_message(chat_id, reply)
    finally:
        conn.close()


def telegram_polling_worker():
    offset = None
    webhook_cleared_for_token = None

    while True:
        try:
            token = (get_telegram_settings().get('telegram_bot_token') or '').strip()
            if not token:
                time.sleep(10)
                continue

            if webhook_cleared_for_token != token:
                try:
                    requests.get(
                        f"https://api.telegram.org/bot{token}/deleteWebhook",
                        params={"drop_pending_updates": False},
                        timeout=10
                    )
                except requests.RequestException as e:
                    app.logger.warning(f"Telegram webhook cleanup failed: {e}")
                webhook_cleared_for_token = token
            params = {'timeout': 20, 'allowed_updates': ['message']}
            if offset is not None: params['offset'] = offset
            resp = requests.get(f'https://api.telegram.org/bot{token}/getUpdates', params=params, timeout=(5, 25))
            if not resp.ok: time.sleep(10); continue
            for update in resp.json().get('result', []):
                offset = update['update_id'] + 1
                msg = update.get('message') or {}
                chat_id = msg.get('chat',{}).get('id'); text = (msg.get('text') or '').strip()
                if chat_id and text: handle_telegram_message(chat_id, text)
        except Exception as e:
            app.logger.error(f'Telegram polling error: {e}')
            time.sleep(10)



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
    data = request.get_json(silent=True) or {}
    token = str(data.get("token") or "").strip()
    conn = get_db()
    try:
        if token:
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES ('telegram_bot_token', ?)",
                (token,)
            )
        conn.commit()
        return jsonify({"success": True})
    finally:
        conn.close()

@app.post("/api/settings/telegram/test")
@require_role('admin')
def test_telegram_connection():
    settings = get_telegram_settings()
    token = (settings.get('telegram_bot_token') or '').strip()

    if not token:
        return jsonify({
            "success": False,
            "message": "أدخل Telegram Bot Token أولاً"
        }), 400

    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{token}/getMe",
            timeout=10
        )

        if resp.ok:
            payload = resp.json()
            bot = payload.get("result") or {}
            bot_name = bot.get("first_name") or bot.get("username") or "البوت"
            return jsonify({
                "success": True,
                "message": f"Telegram متصل بنجاح — {bot_name}"
            })

        try:
            error_text = resp.json().get("description") or resp.text
        except Exception:
            error_text = resp.text

        return jsonify({
            "success": False,
            "message": f"Telegram API error: {error_text}"
        }), 400
    except requests.RequestException as e:
        return jsonify({
            "success": False,
            "message": f"تعذر الاتصال بـ Telegram: {str(e)}"
        }), 500

# --- Notifications status (dashboard indicator) ---
@app.get("/api/notifications/status")
def get_notifications_status():
    conn = get_db()
    try:
        count = conn.execute("SELECT COUNT(*) FROM notifications_log WHERE status='pending'").fetchone()[0]
        return jsonify({"pending_count": count})
    finally:
        conn.close()


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
            payment_status TEXT DEFAULT 'unpaid',
            telegram_chat_id TEXT
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT, 
            student_id TEXT, 
            date TEXT, 
            time TEXT, 
            timestamp DATETIME,
            FOREIGN KEY(student_id) REFERENCES students(student_id)
        )""")
        student_columns = [r['name'] for r in conn.execute('PRAGMA table_info(students)').fetchall()]
        if 'telegram_chat_id' not in student_columns:
            conn.execute('ALTER TABLE students ADD COLUMN telegram_chat_id TEXT')
        if 'monthly_fee' not in student_columns:
            conn.execute('ALTER TABLE students ADD COLUMN monthly_fee REAL DEFAULT 0')

        attendance_columns = [r['name'] for r in conn.execute('PRAGMA table_info(attendance)').fetchall()]
        if 'status' not in attendance_columns:
            conn.execute("ALTER TABLE attendance ADD COLUMN status TEXT DEFAULT 'حاضر'")

        conn.execute("""CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id TEXT,
            amount REAL,
            date TEXT,
            note TEXT,
            created_at DATETIME,
            FOREIGN KEY(student_id) REFERENCES students(student_id)
        )""")

        conn.execute("CREATE TABLE IF NOT EXISTS subjects (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT)")
        conn.execute("""CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE,
            password_hash TEXT,
            role TEXT
        )""")
        # Keep notification history during upgrades; never drop it.
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
        conn.execute("""CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, 
            type TEXT, 
            category TEXT, 
            amount REAL, 
            date TEXT
        )""")
        # --- Newly added tables for previously-missing features ---
        conn.execute("""CREATE TABLE IF NOT EXISTS extra_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id TEXT,
            group_id INTEGER,
            type TEXT,
            subject TEXT,
            date TEXT,
            time TEXT,
            created_at DATETIME,
            FOREIGN KEY(student_id) REFERENCES students(student_id),
            FOREIGN KEY(group_id) REFERENCES groups(id)
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS extra_classes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id TEXT,
            class_name TEXT,
            fees REAL,
            created_at DATETIME,
            FOREIGN KEY(student_id) REFERENCES students(student_id)
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS makeup_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id TEXT,
            makeup_group_id INTEGER,
            status TEXT DEFAULT 'scheduled',
            created_at DATETIME,
            FOREIGN KEY(student_id) REFERENCES students(student_id),
            FOREIGN KEY(makeup_group_id) REFERENCES groups(id)
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS otp_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT,
            code TEXT,
            created_at DATETIME,
            used INTEGER DEFAULT 0
        )""")
        
        conn.execute("INSERT OR IGNORE INTO settings (key,value) VALUES ('telegram_enabled','1')")
        conn.execute("INSERT OR IGNORE INTO settings (key,value) VALUES ('openai_model','gpt-5.6-luna')")

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

@app.post("/api/settings")
@require_role('admin')
def save_general_settings():
    data = request.get_json(silent=True) or {}
    conn = get_db()
    try:
        for key, value in data.items():
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
        conn.commit()
        return jsonify({"success": True})
    finally:
        conn.close()

# --- Finance PIN check (used to unlock the Finances tab) ---
@app.post("/api/check_pin")
@require_role('admin')
def check_pin():
    data = request.get_json(silent=True) or {}
    pin = str(data.get("pin", ""))
    conn = get_db()
    try:
        row = conn.execute("SELECT value FROM settings WHERE key='finance_pin'").fetchone()
        stored_pin = row['value'] if row else None
        if not stored_pin:
            # No PIN configured yet -> allow admin through, but tell them to set one
            return jsonify({"success": True, "message": "لا يوجد رمز PIN معرف بعد"})
        if pin and pin == stored_pin:
            return jsonify({"success": True})
        return jsonify({"success": False, "message": "PIN غير صحيح"}), 401
    finally:
        conn.close()

# --- Finance PIN change: old PIN required for subsequent changes ---
@app.post("/api/settings/finance-pin")
@require_role('admin')
def change_finance_pin():
    data = request.get_json(silent=True) or {}
    old_pin = str(data.get("old_pin") or "")
    new_pin = str(data.get("new_pin") or "").strip()

    if not new_pin:
        return jsonify({"success": False, "message": "أدخل الـ PIN الجديد"}), 400
    if len(new_pin) < 4:
        return jsonify({"success": False, "message": "الـ PIN الجديد يجب أن يكون 4 أرقام على الأقل"}), 400
    if not new_pin.isdigit():
        return jsonify({"success": False, "message": "الـ PIN الجديد يجب أن يتكون من أرقام فقط"}), 400

    conn = get_db()
    try:
        row = conn.execute(
            "SELECT value FROM settings WHERE key='finance_pin'"
        ).fetchone()
        stored_pin = str(row["value"]) if row and row["value"] is not None else ""

        # First setup: no old PIN exists, so allow setting one.
        # Once one exists, changing it requires the current PIN.
        if stored_pin and old_pin != stored_pin:
            return jsonify({
                "success": False,
                "message": "الـ PIN القديم غير صحيح، لا يمكن تغيير PIN المالية"
            }), 401

        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('finance_pin', ?)",
            (new_pin,)
        )
        conn.commit()
        return jsonify({"success": True, "message": "تم تغيير PIN المالية بنجاح"})
    except Exception as e:
        conn.rollback()
        app.logger.error(f"Finance PIN change failed: {e}")
        return jsonify({
            "success": False,
            "message": f"تعذر تغيير PIN المالية: {str(e)}"
        }), 500
    finally:
        conn.close()

# --- Kiosk token regeneration ---
@app.post("/api/settings/kiosk-token")
@require_role('admin')
def regenerate_kiosk_token():
    token = secrets.token_hex(16)
    conn = get_db()
    try:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('kiosk_token', ?)", (token,))
        conn.commit()
        return jsonify({"success": True, "token": token})
    finally:
        conn.close()

# --- Users Management ---
@app.get("/api/users")
@require_role('admin')
def list_users():
    conn = get_db()
    try:
        rows = conn.execute("SELECT id, username, role FROM users").fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()

@app.post("/api/users")
@require_role('admin')
def add_user():
    data = request.get_json(silent=True) or {}
    username = data.get("username")
    password = data.get("password")
    role = data.get("role", "receptionist")
    if not username or not password:
        return jsonify({"success": False, "message": "اسم المستخدم وكلمة المرور مطلوبان"}), 400
    conn = get_db()
    try:
        existing = conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone()
        if existing:
            return jsonify({"success": False, "message": "اسم المستخدم موجود بالفعل"}), 400
        pw_hash = generate_password_hash(password)
        conn.execute("INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)", (username, pw_hash, role))
        conn.commit()
        return jsonify({"success": True})
    finally:
        conn.close()

@app.delete("/api/users/<int:uid>")
@require_role('admin')
def delete_user(uid):
    conn = get_db()
    try:
        conn.execute("DELETE FROM users WHERE id=?", (uid,))
        conn.commit()
        return jsonify({"success": True})
    finally:
        conn.close()

@app.post("/api/setup")
def setup():
    data = request.get_json(silent=True) or {}
    admin_username = str(data.get("admin_username") or "").strip()
    admin_password = str(data.get("admin_password") or "")

    if not admin_username or not admin_password:
        return jsonify({
            "success": False,
            "message": "اسم مستخدم الأدمن وكلمة المرور مطلوبان"
        }), 400

    if len(admin_password) < 6:
        return jsonify({
            "success": False,
            "message": "كلمة المرور يجب أن تكون 6 أحرف على الأقل"
        }), 400

    conn = get_db()
    try:
        for key, value in data.items():
            if key == "admin_password_confirm":
                continue
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (key, value)
            )

        password_hash = generate_password_hash(admin_password)
        existing = conn.execute(
            "SELECT id FROM users WHERE username=?",
            (admin_username,)
        ).fetchone()

        if existing:
            conn.execute(
                "UPDATE users SET password_hash=?, role='admin' WHERE id=?",
                (password_hash, existing["id"])
            )
        else:
            conn.execute(
                "INSERT INTO users (username, password_hash, role) VALUES (?, ?, 'admin')",
                (admin_username, password_hash)
            )

        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('is_setup', '1')"
        )
        conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        conn.rollback()
        app.logger.error(f"Setup failed: {e}")
        return jsonify({
            "success": False,
            "message": f"فشل إعداد النظام: {str(e)}"
        }), 500
    finally:
        conn.close()

@app.get("/api/dashboard")
def get_dashboard():
    conn = get_db()
    try:
        total = conn.execute("SELECT COUNT(*) FROM students").fetchone()[0]
        today = datetime.now().strftime('%Y-%m-%d')
        present = conn.execute("SELECT COUNT(DISTINCT student_id) FROM attendance WHERE date=? AND status='حاضر'", (today,)).fetchone()[0]
        linked_parents = conn.execute("SELECT COUNT(*) FROM students WHERE phone IS NOT NULL AND phone != ''").fetchone()[0]
        recent_rows = conn.execute("""SELECT s.name as name, a.time as time FROM attendance a
                                       JOIN students s ON a.student_id = s.student_id
                                       WHERE a.date=? ORDER BY a.timestamp DESC LIMIT 10""", (today,)).fetchall()
        recent = [dict(r) for r in recent_rows]
        return jsonify({"total_students": total, "present_today": present, "absent_today": total - present,
                         "linked_parents": linked_parents, "recent_attendance": recent})
    finally:
        conn.close()

@app.get("/api/stats/chart")
def get_chart_stats():
    return jsonify([{"date": "اليوم", "count": 0}])

@app.post("/api/attendance")
@require_role(['receptionist', 'admin'])
def register_attendance():
    conn = None
    try:
        body = request.get_json(silent=True) or {}
        sid = body.get("student_id")
        status = body.get("status", "حاضر")
        if not sid:
            return jsonify({"success": False, "message": "كود الطالب مطلوب"}), 400

        conn = get_db()
        student = conn.execute("SELECT name, payment_status FROM students WHERE student_id=?", (sid,)).fetchone()
        if not student:
            return jsonify({"success": False, "message": "الطالب غير موجود"}), 404

        # Duplicate scan guard (only applies to marking present)
        last_scan = conn.execute("SELECT timestamp FROM attendance WHERE student_id=? AND date=? ORDER BY timestamp DESC LIMIT 1", (sid, datetime.now().strftime('%Y-%m-%d'))).fetchone()
        if last_scan and status == "حاضر":
            last_time = datetime.fromisoformat(last_scan['timestamp'])
            diff = datetime.now() - last_time
            if diff < timedelta(seconds=30):
                return jsonify({"success": False, "message": "تم تسجيل الحضور بالفعل مؤخراً"}), 400

        now = datetime.now()
        conn.execute("INSERT INTO attendance (student_id, date, time, timestamp, status) VALUES (?, ?, ?, ?, ?)",
                     (sid, now.strftime('%Y-%m-%d'), now.strftime('%H:%M'), now.isoformat(), status))
        conn.commit()

        # Queue notifications (background)
        if status == "حاضر":
            queue_notification(sid, 'telegram', 'attendance', f"الطالب {student['name']} حضر اليوم.")
            queue_notification(sid, 'whatsapp', 'attendance', f"الطالب {student['name']} حضر اليوم.")
        else:
            queue_notification(sid, 'telegram', 'absence', f"تم تسجيل غياب الطالب {student['name']} اليوم.")

        return jsonify({"success": True, "studentName": student['name'], "payment_status": student['payment_status']})
    except Exception as e:
        log_crash(e)
        app.logger.error(f"register_attendance failed: {e}")
        return jsonify({"success": False, "message": "حدث خطأ غير متوقع أثناء تسجيل الحضور"}), 500
    finally:
        if conn:
            conn.close()

@app.get("/api/students")
def list_students():
    conn = get_db()
    try:
        page = request.args.get('page', default=1, type=int)
        limit = request.args.get('limit', default=50, type=int)
        total = conn.execute("SELECT COUNT(*) FROM students").fetchone()[0]
        offset = (page - 1) * limit
        rows = conn.execute("SELECT * FROM students LIMIT ? OFFSET ?", (limit, offset)).fetchall()
        return jsonify({"students": [dict(r) for r in rows], "total": total, "limit": limit, "page": page})
    finally:
        conn.close()

# --- Full student profile (used by the profile modal) ---
@app.get("/api/students/profile/<sid>")
def get_student_profile(sid):
    conn = get_db()
    try:
        student = conn.execute("SELECT * FROM students WHERE student_id=?", (sid,)).fetchone()
        if not student:
            return jsonify({"success": False, "message": "الطالب غير موجود"}), 404

        attendance_count = conn.execute("SELECT COUNT(*) FROM attendance WHERE student_id=?", (sid,)).fetchone()[0]
        grades_rows = conn.execute("""SELECT g.*, s.name as subject_name FROM grades g
                                       JOIN subjects s ON g.subject_id = s.id
                                       WHERE g.student_id=? ORDER BY g.date DESC""", (sid,)).fetchall()
        return jsonify({
            "student": dict(student),
            "attendance_count": attendance_count,
            "grades": [dict(r) for r in grades_rows]
        })
    finally:
        conn.close()

REQUIRED_STUDENT_FIELDS = ['name', 'level', 'group_name', 'parent_name', 'phone']

@app.post("/api/students")
def add_student():
    data = request.get_json(silent=True) or {}
    missing = [f for f in ['student_id'] + REQUIRED_STUDENT_FIELDS if not data.get(f)]
    if missing:
        return jsonify({"success": False, "message": f"الحقول التالية مطلوبة: {', '.join(missing)}"}), 400
    conn = get_db()
    try:
        conn.execute("INSERT INTO students (student_id, name, level, group_name, parent_name, phone) VALUES (?,?,?,?,?,?)",
                     (data['student_id'], data['name'], data['level'], data['group_name'], data['parent_name'], data['phone']))
        conn.commit()
        return jsonify({"success": True})
    finally:
        conn.close()

@app.post("/api/students/import")
@require_role('admin')
def import_students():
    if 'file' not in request.files:
        return jsonify({"success": False, "message": "لم يتم العثور على ملف"}), 400

    file = request.files['file']
    if not file.filename.endswith('.xlsx'):
        return jsonify({"success": False, "message": "امتداد الملف يجب أن يكون .xlsx"}), 400

    import openpyxl
    try:
        wb = openpyxl.load_workbook(file)
        sheet = wb.active

        imported = 0
        skipped = 0
        errors = []
        conn = get_db()
        cursor = conn.cursor()

        for row_idx, row in enumerate(sheet.iter_rows(min_row=2, values_only=True), start=2):
            sid, name, level, gname, pname, phone = row

            if not sid or not name:
                errors.append({"row": row_idx, "reason": "الاسم أو كود الطالب مفقود"})
                continue

            exists = cursor.execute("SELECT 1 FROM students WHERE student_id=?", (str(sid),)).fetchone()
            if exists:
                skipped += 1
                continue

            cursor.execute("INSERT INTO students (student_id, name, level, group_name, parent_name, phone) VALUES (?,?,?,?,?,?)",
                           (str(sid), str(name), str(level or ''), str(gname or ''), str(pname or ''), str(phone or '')))
            imported += 1
        conn.commit()
        conn.close()
        return jsonify({"success": True, "imported": imported, "skipped_duplicates": skipped, "errors": errors})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 400

@app.put("/api/students/<sid>")
def update_student(sid):
    data = request.get_json(silent=True) or {}

    # Payment status toggle (partial update, no other fields required)
    if set(data.keys()) == {'payment_status'} and data.get('payment_status') == 'toggle':
        conn = get_db()
        try:
            student = conn.execute("SELECT payment_status FROM students WHERE student_id=?", (sid,)).fetchone()
            if not student:
                return jsonify({"success": False, "message": "الطالب غير موجود"}), 404
            new_status = 'unpaid' if student['payment_status'] == 'paid' else 'paid'
            conn.execute("UPDATE students SET payment_status=? WHERE student_id=?", (new_status, sid))
            conn.commit()
            return jsonify({"success": True, "payment_status": new_status})
        finally:
            conn.close()

    missing = [f for f in REQUIRED_STUDENT_FIELDS if not data.get(f)]
    if missing:
        return jsonify({"success": False, "message": f"الحقول التالية مطلوبة: {', '.join(missing)}"}), 400
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
    font_name = 'Arabic' if ARABIC_FONT_LOADED else 'Helvetica'
    c.setFont(font_name, 14)
    c.drawRightString(500, 750, ar(f"الطالب: {student['name']}"))
    c.drawRightString(500, 730, ar(f"الكود: {student['student_id']}"))
    c.drawRightString(500, 710, ar(f"الشهر: {month}"))
    c.drawRightString(500, 690, ar(f"عدد مرات الحضور: {attendance}"))
    c.drawRightString(500, 670, ar(f"متوسط الدرجات: {grades:.2f}"))
    c.drawRightString(500, 650, ar(f"حالة الدفع: {student['payment_status']}"))
    c.save()
    buffer.seek(0)
    return send_file(buffer, mimetype='application/pdf', download_name=f"report_{student_id}.pdf")

# --- General attendance/finance report export (used by exportReport in the UI) ---
@app.get("/api/reports/<report_type>/pdf")
@require_role('admin')
def get_generic_pdf_report(report_type):
    month = request.args.get('month', datetime.now().strftime('%Y-%m'))
    conn = get_db()
    try:
        from reportlab.pdfgen import canvas
        buffer = io.BytesIO()
        c = canvas.Canvas(buffer)
        font_name = 'Arabic' if ARABIC_FONT_LOADED else 'Helvetica'
        c.setFont(font_name, 14)
        y = 750

        if report_type == 'attendance':
            c.drawRightString(500, y, ar(f"تقرير الحضور - {month}"))
            y -= 30
            rows = conn.execute("""SELECT s.name, COUNT(a.id) as cnt FROM students s
                                    LEFT JOIN attendance a ON a.student_id = s.student_id AND a.date LIKE ?
                                    GROUP BY s.student_id""", (f"{month}%",)).fetchall()
            for r in rows:
                c.drawRightString(500, y, ar(f"{r['name']}: {r['cnt']}"))
                y -= 20
                if y < 50:
                    c.showPage()
                    c.setFont(font_name, 14)
                    y = 750
        elif report_type == 'finance':
            c.drawRightString(500, y, ar(f"التقرير المالي - {month}"))
            y -= 30
            rows = conn.execute("SELECT * FROM transactions WHERE date LIKE ?", (f"{month}%",)).fetchall()
            for r in rows:
                c.drawRightString(500, y, ar(f"{r['type']} | {r['category']} | {r['amount']} | {r['date']}"))
                y -= 20
                if y < 50:
                    c.showPage()
                    c.setFont(font_name, 14)
                    y = 750
        else:
            return jsonify({"success": False, "message": "نوع التقرير غير معروف"}), 400

        c.save()
        buffer.seek(0)
        return send_file(buffer, mimetype='application/pdf', download_name=f"{report_type}_report_{month}.pdf")
    finally:
        conn.close()

# Groups Management
@app.get("/api/groups")
def list_groups():
    conn = get_db()
    try:
        rows = conn.execute("SELECT * FROM groups").fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()

@app.post("/api/groups")
@require_role('admin')
def create_group():
    data = request.get_json()
    conn = get_db()
    try:
        conn.execute("INSERT INTO groups (name, day, time, max_capacity, teacher_id) VALUES (?,?,?,?,?)",
                     (data['name'], data['day'], data['time'], data.get('max_capacity'), data.get('teacher_id')))
        conn.commit()
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
        if group['max_capacity'] and count >= group['max_capacity']:
            return jsonify({"success": False, "message": "Group is full"}), 400

        conn.execute("INSERT INTO student_groups (group_id, student_id) VALUES (?, ?)", (gid, sid))
        conn.commit()
        return jsonify({"success": True})
    finally:
        conn.close()

# --- Group details: roster + today's attendance/payment status ---
@app.get("/api/groups/<int:gid>/details")
def get_group_details(gid):
    conn = get_db()
    try:
        group = conn.execute("SELECT * FROM groups WHERE id=?", (gid,)).fetchone()
        if not group:
            return jsonify({"success": False, "message": "المجموعة غير موجودة"}), 404

        today = datetime.now().strftime('%Y-%m-%d')
        students_rows = conn.execute("""
            SELECT s.student_id, s.name, s.payment_status,
                   EXISTS(SELECT 1 FROM attendance a WHERE a.student_id = s.student_id AND a.date = ? AND a.status='حاضر') as is_present
            FROM student_groups sg
            JOIN students s ON sg.student_id = s.student_id
            WHERE sg.group_id = ?
        """, (today, gid)).fetchall()

        students = [dict(r) for r in students_rows]
        total = len(students)
        present_count = sum(1 for s in students if s['is_present'])
        paid_count = sum(1 for s in students if s['payment_status'] == 'paid')
        attendance_rate = f"{round((present_count / total) * 100)}%" if total else "0%"

        return jsonify({
            "students": students,
            "stats": {
                "total": total,
                "attendance_rate": attendance_rate,
                "paid_count": paid_count
            }
        })
    finally:
        conn.close()

# --- Group attendance report (per-student attendance counts) ---
@app.get("/api/groups/report/<int:gid>")
def get_group_report(gid):
    conn = get_db()
    try:
        group = conn.execute("SELECT * FROM groups WHERE id=?", (gid,)).fetchone()
        if not group:
            return jsonify({"success": False, "message": "المجموعة غير موجودة"}), 404

        records_rows = conn.execute("""
            SELECT s.name as name, COUNT(a.id) as attendance_count
            FROM student_groups sg
            JOIN students s ON sg.student_id = s.student_id
            LEFT JOIN attendance a ON a.student_id = s.student_id
            WHERE sg.group_id = ?
            GROUP BY s.student_id
        """, (gid,)).fetchall()

        return jsonify({
            "group": group['name'],
            "records": [dict(r) for r in records_rows]
        })
    finally:
        conn.close()

# --- Extra classes registration ---
@app.post("/api/extra-classes/register")
def register_extra_class():
    data = request.get_json(silent=True) or {}
    sid = data.get("student_id")
    class_name = data.get("class_name")
    fees = data.get("fees") or 0
    if not sid or not class_name:
        return jsonify({"success": False, "message": "بيانات ناقصة"}), 400
    conn = get_db()
    try:
        conn.execute("INSERT INTO extra_classes (student_id, class_name, fees, created_at) VALUES (?,?,?,?)",
                     (sid, class_name, fees, datetime.now().isoformat()))
        conn.commit()
        return jsonify({"success": True})
    finally:
        conn.close()

# --- Extra / makeup session scheduling (calendar tool in the UI) ---
@app.post("/api/extra-sessions")
def schedule_extra_session():
    data = request.get_json(silent=True) or {}
    subject = data.get("subject")
    date = data.get("date")
    time_ = data.get("time")
    if not subject or not date or not time_:
        return jsonify({"success": False, "message": "أدخل بيانات كاملة"}), 400

    sid = data.get("student_id")
    gid = data.get("group_id")
    conn = get_db()
    try:
        conn.execute("""INSERT INTO extra_sessions (student_id, group_id, type, subject, date, time, created_at)
                         VALUES (?,?,?,?,?,?,?)""",
                     (sid, gid, data.get("type"), subject, date, time_, datetime.now().isoformat()))
        conn.commit()

        # Notify affected students
        targets = []
        if sid:
            targets.append(sid)
        elif gid:
            rows = conn.execute("SELECT student_id FROM student_groups WHERE group_id=?", (gid,)).fetchall()
            targets = [r['student_id'] for r in rows]

        for target_sid in targets:
            queue_notification(target_sid, 'telegram', 'extra_session',
                                f"تم جدولة حصة {subject} بتاريخ {date} الساعة {time_}.")

        return jsonify({"success": True})
    finally:
        conn.close()

# --- Makeup sessions: find suitable groups for a student ---
def _get_makeup_candidates(conn, sid):
    student = conn.execute("SELECT * FROM students WHERE student_id=?", (sid,)).fetchone()
    if not student:
        return None
    # Suggest groups the student isn't already a member of
    rows = conn.execute("""
        SELECT g.* FROM groups g
        WHERE g.id NOT IN (SELECT group_id FROM student_groups WHERE student_id=?)
    """, (sid,)).fetchall()
    return [dict(r) for r in rows]

@app.get("/api/makeup/available-groups/<sid>")
def get_makeup_available_groups(sid):
    conn = get_db()
    try:
        groups = _get_makeup_candidates(conn, sid)
        if groups is None:
            return jsonify({"success": False, "message": "الطالب غير موجود"}), 404
        return jsonify(groups)
    finally:
        conn.close()

@app.get("/api/makeup/find/<sid>")
def find_makeup_groups(sid):
    conn = get_db()
    try:
        groups = _get_makeup_candidates(conn, sid)
        if groups is None:
            return jsonify({"success": False, "message": "الطالب غير موجود"}), 404
        return jsonify(groups)
    finally:
        conn.close()

@app.post("/api/makeup/register")
def register_makeup():
    data = request.get_json(silent=True) or {}
    sid = data.get("student_id")
    gid = data.get("makeup_group_id")
    if not sid or not gid:
        return jsonify({"success": False, "message": "يرجى اختيار مجموعة"}), 400
    conn = get_db()
    try:
        conn.execute("""INSERT INTO makeup_sessions (student_id, makeup_group_id, status, created_at)
                         VALUES (?,?, 'scheduled', ?)""", (sid, gid, datetime.now().isoformat()))
        conn.commit()
        queue_notification(sid, 'telegram', 'makeup', "تم تسجيلك في حصة تعويض جديدة.")
        return jsonify({"success": True})
    finally:
        conn.close()

# --- Payment update (guarded by finance PIN) ---
@app.post("/api/payments/update")
def update_payment():
    data = request.get_json(silent=True) or {}
    sid = data.get("student_id")
    status = data.get("status")
    pin = str(data.get("pin", ""))

    if not sid or status not in ("paid", "unpaid"):
        return jsonify({"success": False, "message": "بيانات ناقصة"}), 400

    conn = get_db()
    try:
        row = conn.execute("SELECT value FROM settings WHERE key='finance_pin'").fetchone()
        stored_pin = row['value'] if row else None
        if stored_pin and pin != stored_pin:
            return jsonify({"success": False, "message": "PIN غير صحيح"}), 401

        student = conn.execute("SELECT 1 FROM students WHERE student_id=?", (sid,)).fetchone()
        if not student:
            return jsonify({"success": False, "message": "الطالب غير موجود"}), 404

        conn.execute("UPDATE students SET payment_status=? WHERE student_id=?", (status, sid))
        conn.commit()
        return jsonify({"success": True})
    finally:
        conn.close()

# --- Real payments ledger (monthly fee + payment history), additive to
#     the existing paid/unpaid toggle above ---
@app.post("/api/payments/record")
@require_role('admin')
def record_payment():
    data = request.get_json(silent=True) or {}
    sid = data.get("student_id")
    amount = data.get("amount")
    pin = str(data.get("pin", ""))
    if not sid or amount is None:
        return jsonify({"success": False, "message": "بيانات ناقصة"}), 400

    conn = get_db()
    try:
        row = conn.execute("SELECT value FROM settings WHERE key='finance_pin'").fetchone()
        stored_pin = row['value'] if row else None
        if stored_pin and pin != stored_pin:
            return jsonify({"success": False, "message": "PIN غير صحيح"}), 401

        student = conn.execute("SELECT 1 FROM students WHERE student_id=?", (sid,)).fetchone()
        if not student:
            return jsonify({"success": False, "message": "الطالب غير موجود"}), 404

        conn.execute(
            "INSERT INTO payments (student_id, amount, date, note, created_at) VALUES (?,?,?,?,?)",
            (sid, amount, datetime.now().strftime('%Y-%m-%d'), data.get('note', ''), datetime.now().isoformat())
        )
        conn.execute(
            "INSERT INTO transactions (type, category, amount, date) VALUES ('income','اشتراك طالب',?,?)",
            (amount, datetime.now().strftime('%Y-%m-%d'))
        )
        conn.commit()
        return jsonify({"success": True})
    finally:
        conn.close()

@app.get("/api/students/<sid>/balance")
def get_student_balance(sid):
    conn = get_db()
    try:
        student = conn.execute("SELECT monthly_fee FROM students WHERE student_id=?", (sid,)).fetchone()
        if not student:
            return jsonify({"success": False, "message": "الطالب غير موجود"}), 404
        total_paid = conn.execute(
            "SELECT COALESCE(SUM(amount),0) FROM payments WHERE student_id=?", (sid,)
        ).fetchone()[0]
        payments_history = conn.execute(
            "SELECT amount, date, note FROM payments WHERE student_id=? ORDER BY date DESC", (sid,)
        ).fetchall()
        return jsonify({
            "monthly_fee": student['monthly_fee'] or 0,
            "total_paid": total_paid,
            "payments": [dict(r) for r in payments_history]
        })
    finally:
        conn.close()

# --- OTP generation (simple placeholder implementation) ---
@app.post("/api/otp/generate")
def generate_otp():
    data = request.get_json(silent=True) or {}
    phone = data.get("phone")
    if not phone:
        return jsonify({"success": False, "message": "رقم الهاتف مطلوب"}), 400

    code = f"{secrets.randbelow(900000) + 100000}"
    conn = get_db()
    try:
        conn.execute("INSERT INTO otp_codes (phone, code, created_at, used) VALUES (?,?,?,0)",
                     (phone, code, datetime.now().isoformat()))
        conn.commit()
        return jsonify({"success": True, "code": code})
    finally:
        conn.close()

# =========================================================
# NEW: Multi-source student import — Excel / PDF / Voice
# Preview -> Validation -> Confirmation workflow.
# These are ADDITIVE endpoints; the original /api/students/import
# (direct-write Excel import) above is left completely untouched.
# =========================================================

def _validate_student_row(row, seen_ids, existing_ids, existing_groups):
    """Shared validation used by the excel/pdf/voice preview endpoints.
    Returns (is_valid, list_of_error_strings, warnings_list)."""
    errors = []
    warnings = []

    sid = str(row.get('student_id') or '').strip()
    name = str(row.get('name') or '').strip()
    phone = str(row.get('phone') or '').strip()
    group_name = str(row.get('group_name') or '').strip()

    if not sid:
        errors.append("كود الطالب فارغ")
    if not name:
        errors.append("اسم الطالب فارغ")

    if sid:
        if sid in seen_ids:
            errors.append("كود مكرر داخل الملف")
        elif sid in existing_ids:
            errors.append("الكود موجود بالفعل في النظام")

    if phone:
        digits = ''.join(ch for ch in phone if ch.isdigit())
        if len(digits) < 8 or len(digits) > 15:
            errors.append("رقم الهاتف غير صحيح")
    else:
        warnings.append("رقم الهاتف مفقود")

    if group_name and existing_groups and group_name not in existing_groups:
        warnings.append("المجموعة غير موجودة مسبقاً (سيتم حفظها كنص فقط)")

    return (len(errors) == 0, errors, warnings)


def _get_existing_ids_and_groups(conn):
    existing_ids = {r['student_id'] for r in conn.execute("SELECT student_id FROM students").fetchall()}
    existing_groups = {r['name'] for r in conn.execute("SELECT name FROM groups").fetchall()}
    return existing_ids, existing_groups


@app.post("/api/students/import/excel/preview")
@require_role('admin')
def preview_excel_import():
    """Parses the uploaded .xlsx WITHOUT writing to the DB and returns a
    row-by-row preview with validation, for the new preview->confirm UI."""
    if 'file' not in request.files:
        return jsonify({"success": False, "message": "لم يتم العثور على ملف"}), 400

    file = request.files['file']
    if not file.filename.endswith('.xlsx'):
        return jsonify({"success": False, "message": "امتداد الملف يجب أن يكون .xlsx"}), 400

    try:
        import openpyxl
        wb = openpyxl.load_workbook(file, data_only=True)
        sheet = wb.active

        conn = get_db()
        existing_ids, existing_groups = _get_existing_ids_and_groups(conn)
        conn.close()

        seen_ids = set()
        rows_out = []
        for row_idx, row in enumerate(sheet.iter_rows(min_row=2, values_only=True), start=2):
            if row is None or all(v is None or str(v).strip() == '' for v in row):
                continue
            padded = list(row) + [None] * (6 - len(row))
            sid, name, level, gname, pname, phone = padded[:6]
            record = {
                "student_id": str(sid).strip() if sid is not None else "",
                "name": str(name).strip() if name is not None else "",
                "level": str(level).strip() if level is not None else "",
                "group_name": str(gname).strip() if gname is not None else "",
                "parent_name": str(pname).strip() if pname is not None else "",
                "phone": str(phone).strip() if phone is not None else "",
            }
            valid, errors, warnings = _validate_student_row(record, seen_ids, existing_ids, existing_groups)
            if record["student_id"]:
                seen_ids.add(record["student_id"])
            rows_out.append({"row": row_idx, **record, "valid": valid, "errors": errors, "warnings": warnings})

        return jsonify({"success": True, "rows": rows_out,
                         "valid_count": sum(1 for r in rows_out if r["valid"]),
                         "invalid_count": sum(1 for r in rows_out if not r["valid"])})
    except Exception as e:
        return jsonify({"success": False, "message": f"تعذرت قراءة ملف Excel: {str(e)}"}), 400


@app.post("/api/students/import/pdf/preview")
@require_role('admin')
def preview_pdf_import():
    """قراءة أسماء الطلاب والأكواد من PDF وعرضها للمعاينة قبل الحفظ."""

    if 'file' not in request.files:
        return jsonify({
            "success": False,
            "message": "لم يتم العثور على ملف PDF"
        }), 400

    file = request.files['file']

    if not file.filename:
        return jsonify({
            "success": False,
            "message": "لم يتم اختيار ملف"
        }), 400

    if not file.filename.lower().endswith(".pdf"):
        return jsonify({
            "success": False,
            "message": "من فضلك اختر ملف PDF فقط"
        }), 400

    try:
        import pdfplumber
    except ImportError:
        return jsonify({
            "success": False,
            "message": "مكتبة pdfplumber غير مثبتة. شغل: pip install pdfplumber"
        }), 501

    def normalize_digits(text):
        if text is None:
            return ""

        text = str(text)

        arabic = "٠١٢٣٤٥٦٧٨٩"
        english = "0123456789"

        table = str.maketrans(arabic, english)

        return text.translate(table)

    def clean_text(text):
        if text is None:
            return ""

        text = str(text)
        text = text.replace("\u00a0", " ")
        text = re.sub(r"\s+", " ", text)

        return text.strip(" \t\r\n|:-–—")

    def is_student_code(text):
        text = normalize_digits(clean_text(text))

        if not text:
            return False

        # كود رقمي مثل 1001
        if re.fullmatch(r"\d{2,20}", text):
            return True

        # كود مثل ST001
        if re.fullmatch(r"[A-Za-z]{1,8}[-_]?\d{2,20}", text):
            return True

        return False

    def create_record(student_id, name):
        return {
            "student_id": normalize_digits(clean_text(student_id)),
            "name": clean_text(name),
            "level": "",
            "group_name": "",
            "parent_name": "",
            "phone": ""
        }

    def parse_line(line):
        line = clean_text(line)

        if not line:
            return None

        line = normalize_digits(line)

        # -----------------------------
        # 1001 أحمد محمد
        # -----------------------------
        match = re.match(
            r"^\s*(\d{2,20})\s+(.+?)\s*$",
            line
        )

        if match:
            student_id = match.group(1)
            name = match.group(2).strip()

            if name and not is_student_code(name):
                return create_record(student_id, name)

        # -----------------------------
        # أحمد محمد 1001
        # -----------------------------
        match = re.match(
            r"^\s*(.+?)\s+(\d{2,20})\s*$",
            line
        )

        if match:
            name = match.group(1).strip()
            student_id = match.group(2)

            if name and not is_student_code(name):
                return create_record(student_id, name)

        # -----------------------------
        # 1001 - أحمد محمد
        # -----------------------------
        match = re.match(
            r"^\s*(\d{2,20})\s*[-–—:|]\s*(.+?)\s*$",
            line
        )

        if match:
            return create_record(
                match.group(1),
                match.group(2)
            )

        # -----------------------------
        # أحمد محمد - 1001
        # -----------------------------
        match = re.match(
            r"^\s*(.+?)\s*[-–—:|]\s*(\d{2,20})\s*$",
            line
        )

        if match:
            return create_record(
                match.group(2),
                match.group(1)
            )

        return None

    try:

        conn = get_db()

        try:
            existing_ids, existing_groups = \
                _get_existing_ids_and_groups(conn)
        finally:
            conn.close()

        rows_out = []
        seen_ids = set()

        file_bytes = io.BytesIO(file.read())

        with pdfplumber.open(file_bytes) as pdf:

            for page in pdf.pages:

                # ==========================================
                # الطريقة الأولى: استخراج الجداول
                # ==========================================

                try:
                    tables = page.extract_tables()
                except Exception:
                    tables = []

                for table in tables or []:

                    for raw_row in table:

                        if not raw_row:
                            continue

                        cells = [
                            clean_text(cell)
                            for cell in raw_row
                            if cell is not None
                            and clean_text(cell)
                        ]

                        if not cells:
                            continue

                        # تجاهل Header
                        joined = " ".join(cells)

                        if "كود" in joined and "اسم" in joined:
                            continue

                        student_id = ""
                        name = ""

                        # البحث عن الكود
                        for cell in cells:
                            if is_student_code(cell):
                                student_id = cell
                                break

                        if not student_id:
                            continue

                        # الاسم = أطول خانة نصية غير الكود
                        names = [
                            cell for cell in cells
                            if cell != student_id
                            and not is_student_code(cell)
                        ]

                        if not names:
                            continue

                        name = max(names, key=len)

                        record = create_record(
                            student_id,
                            name
                        )

                        sid = record["student_id"]

                        if not sid or sid in seen_ids:
                            continue

                        seen_ids.add(sid)

                        valid, errors, warnings = \
                            _validate_student_row(
                                record,
                                seen_ids,
                                existing_ids,
                                existing_groups
                            )

                        rows_out.append({
                            "row": len(rows_out) + 1,
                            **record,
                            "valid": valid,
                            "errors": errors,
                            "warnings": warnings
                        })

                # ==========================================
                # الطريقة الثانية: PDF نصي عادي
                # ==========================================

                try:
                    text = page.extract_text()
                except Exception:
                    text = None

                if text:

                    for line in text.splitlines():

                        record = parse_line(line)

                        if not record:
                            continue

                        sid = record["student_id"]

                        if not sid or sid in seen_ids:
                            continue

                        seen_ids.add(sid)

                        valid, errors, warnings = \
                            _validate_student_row(
                                record,
                                seen_ids,
                                existing_ids,
                                existing_groups
                            )

                        rows_out.append({
                            "row": len(rows_out) + 1,
                            **record,
                            "valid": valid,
                            "errors": errors,
                            "warnings": warnings
                        })

        if not rows_out:
            return jsonify({
                "success": False,
                "message": (
                    "لم أستطع استخراج أسماء الطلاب والأكواد من الـPDF. "
                    "إذا كان الملف عبارة عن صور، سنحتاج OCR."
                )
            }), 400

        return jsonify({
            "success": True,
            "rows": rows_out,
            "valid_count": sum(
                1 for row in rows_out
                if row["valid"]
            ),
            "invalid_count": sum(
                1 for row in rows_out
                if not row["valid"]
            )
        })

    except Exception as e:

        app.logger.exception(
            "PDF import error"
        )

        return jsonify({
            "success": False,
            "message": f"تعذرت قراءة ملف PDF: {str(e)}"
        }), 400
@app.post("/api/students/bulk/confirm")
@require_role('admin')
def confirm_bulk_students():
    """Final confirmation step for Excel / PDF / Voice imports.
    Re-validates every row server-side (never trusts the client preview)
    and inserts only the valid, non-duplicate rows using the SAME
    students table / schema as the original single-student add endpoint.
    Does not touch or replace /api/students or /api/students/import."""
    data = request.get_json(silent=True) or {}
    rows = data.get("students") or []
    if not isinstance(rows, list) or not rows:
        return jsonify({"success": False, "message": "لا توجد بيانات لإضافتها"}), 400

    conn = get_db()
    try:
        existing_ids, existing_groups = _get_existing_ids_and_groups(conn)
        seen_ids = set()
        inserted = 0
        skipped = []

        for row in rows:
            valid, errors, _warnings = _validate_student_row(row, seen_ids, existing_ids, existing_groups)
            sid = str(row.get('student_id') or '').strip()
            if not valid:
                skipped.append({"student_id": sid, "reason": '، '.join(errors)})
                continue

            conn.execute(
                "INSERT INTO students (student_id, name, level, group_name, parent_name, phone) VALUES (?,?,?,?,?,?)",
                (sid, str(row.get('name') or '').strip(), str(row.get('level') or '').strip(),
                 str(row.get('group_name') or '').strip(), str(row.get('parent_name') or '').strip(),
                 str(row.get('phone') or '').strip())
            )
            seen_ids.add(sid)
            existing_ids.add(sid)
            inserted += 1

        conn.commit()
        return jsonify({"success": True, "inserted": inserted, "skipped": skipped})
    finally:
        conn.close()

# =========================================================
# AI ASSISTANT - DATABASE AWARE
# =========================================================

def _ai_db_context(question):
    """
    Builds a focused context from the REAL SQLite database
    according to the user's question.
    """
    conn = get_db()

    try:
        today = datetime.now().strftime('%Y-%m-%d')
        month = datetime.now().strftime('%Y-%m')

        # -------------------------------------------------
        # Basic system statistics
        # -------------------------------------------------

        total_students = conn.execute("""
            SELECT COUNT(*)
            FROM students
            WHERE active_status = 1
        """).fetchone()[0]

        present_today = conn.execute("""
            SELECT COUNT(DISTINCT student_id)
            FROM attendance
            WHERE date = ? AND status = 'حاضر'
        """, (today,)).fetchone()[0]

        absent_today = max(total_students - present_today, 0)

        # -------------------------------------------------
        # Groups
        # -------------------------------------------------

        groups = conn.execute("""
            SELECT
                g.id,
                g.name,
                g.day,
                g.time,
                g.max_capacity,
                COUNT(sg.student_id) AS student_count
            FROM groups g
            LEFT JOIN student_groups sg
                ON sg.group_id = g.id
            GROUP BY g.id
            ORDER BY g.name
        """).fetchall()

        groups_data = [dict(r) for r in groups]

        # -------------------------------------------------
        # Today's attendance
        # -------------------------------------------------

        today_rows = conn.execute("""
            SELECT
                s.student_id,
                s.name,
                s.level,
                s.group_name,
                a.time,
                a.status
            FROM attendance a
            JOIN students s
                ON s.student_id = a.student_id
            WHERE a.date = ?
            ORDER BY a.time DESC
        """, (today,)).fetchall()

        today_attendance = [dict(r) for r in today_rows]

        # -------------------------------------------------
        # Students
        # -------------------------------------------------

        students = conn.execute("""
            SELECT
                student_id,
                name,
                level,
                group_name,
                parent_name,
                phone,
                active_status,
                payment_status
            FROM students
            WHERE active_status = 1
            ORDER BY name
        """).fetchall()

        students_data = [dict(r) for r in students]

        # -------------------------------------------------
        # Monthly attendance statistics
        # -------------------------------------------------

        monthly_attendance = conn.execute("""
            SELECT
                s.student_id,
                s.name,
                s.level,
                s.group_name,
                COUNT(a.id) AS attendance_count
            FROM students s
            LEFT JOIN attendance a
                ON a.student_id = s.student_id
                AND a.date LIKE ?
                AND a.status = 'حاضر'
            WHERE s.active_status = 1
            GROUP BY
                s.student_id,
                s.name,
                s.level,
                s.group_name
            ORDER BY attendance_count ASC
        """, (month + '%',)).fetchall()

        monthly_data = [dict(r) for r in monthly_attendance]

        # -------------------------------------------------
        # Finance
        # -------------------------------------------------

        finance_rows = conn.execute("""
            SELECT
                type,
                category,
                amount,
                date
            FROM transactions
            WHERE date LIKE ?
            ORDER BY date DESC
        """, (month + '%',)).fetchall()

        finance_data = [dict(r) for r in finance_rows]

        revenue = conn.execute("""
            SELECT COALESCE(SUM(amount), 0)
            FROM transactions
            WHERE type = 'income'
            AND date LIKE ?
        """, (month + '%',)).fetchone()[0]

        expenses = conn.execute("""
            SELECT COALESCE(SUM(amount), 0)
            FROM transactions
            WHERE type = 'expense'
            AND date LIKE ?
        """, (month + '%',)).fetchone()[0]

        # -------------------------------------------------
        # Unpaid students
        # -------------------------------------------------

        unpaid = conn.execute("""
            SELECT
                student_id,
                name,
                level,
                group_name,
                parent_name,
                phone
            FROM students
            WHERE active_status = 1
            AND COALESCE(payment_status, 'unpaid') = 'unpaid'
            ORDER BY name
        """).fetchall()

        unpaid_data = [dict(r) for r in unpaid]

        # -------------------------------------------------
        # Attendance by group
        # -------------------------------------------------

        group_attendance = conn.execute("""
            SELECT
                g.name AS group_name,
                COUNT(DISTINCT sg.student_id) AS total_students,
                COUNT(DISTINCT CASE
                    WHEN a.date = ? THEN a.student_id
                END) AS present_today
            FROM groups g
            LEFT JOIN student_groups sg
                ON sg.group_id = g.id
            LEFT JOIN attendance a
                ON a.student_id = sg.student_id
                AND a.date = ?
            GROUP BY g.id
            ORDER BY g.name
        """, (today, today)).fetchall()

        group_attendance_data = [
            dict(r) for r in group_attendance
        ]

        # -------------------------------------------------
        # Student-specific search
        # -------------------------------------------------

        question_lower = question.lower()

        student_matches = []

        # Try exact student ID first
        exact_id = conn.execute("""
            SELECT
                student_id,
                name,
                level,
                group_name,
                parent_name,
                phone,
                payment_status,
                active_status
            FROM students
            WHERE student_id = ?
            LIMIT 1
        """, (question.strip(),)).fetchone()

        if exact_id:
            student_matches.append(dict(exact_id))

        # Search by name words
        words = [
            w.strip()
            for w in question.replace('؟', ' ').split()
            if len(w.strip()) >= 3
        ]

        for word in words[:5]:
            rows = conn.execute("""
                SELECT
                    student_id,
                    name,
                    level,
                    group_name,
                    parent_name,
                    phone,
                    payment_status,
                    active_status
                FROM students
                WHERE name LIKE ?
                LIMIT 10
            """, ('%' + word + '%',)).fetchall()

            for r in rows:
                item = dict(r)

                if not any(
                    x['student_id'] == item['student_id']
                    for x in student_matches
                ):
                    student_matches.append(item)

        # -------------------------------------------------
        # Selected student's attendance history
        # -------------------------------------------------

        student_attendance = []

        for student in student_matches[:10]:
            rows = conn.execute("""
                SELECT
                    date,
                    time,
                    status
                FROM attendance
                WHERE student_id = ?
                ORDER BY date DESC, time DESC
                LIMIT 100
            """, (student['student_id'],)).fetchall()

            student_attendance.append({
                "student_id": student['student_id'],
                "name": student['name'],
                "records": [dict(r) for r in rows]
            })

        # -------------------------------------------------
        # Final context
        # -------------------------------------------------

        context = {
            "current_date": today,
            "current_month": month,

            "system": {
                "total_active_students": total_students,
                "present_today": present_today,
                "absent_today": absent_today
            },

            "students": students_data,

            "today_attendance": today_attendance,

            "monthly_attendance": monthly_data,

            "student_matches": student_matches,

            "student_attendance_history": student_attendance,

            "groups": groups_data,

            "group_attendance_today": group_attendance_data,

            "unpaid_students": unpaid_data,

            "finance": {
                "month": month,
                "revenue": revenue,
                "expenses": expenses,
                "net_profit": revenue - expenses,
                "transactions": finance_data
            }
        }

        return context

    finally:
        conn.close()


@app.post("/api/ai/assistant")
@require_role(['admin', 'teacher', 'receptionist'])
def ai_assistant():

    data = request.get_json(silent=True) or {}

    question = str(
        data.get('question') or ''
    ).strip()

    if not question:
        return jsonify({
            "success": False,
            "message": "اكتب سؤالك أولاً"
        }), 400

    # -------------------------------------------------
    # Build REAL database context
    # -------------------------------------------------

    try:
        context = _ai_db_context(question)
    except Exception as e:

        app.logger.error(
            f"AI database context error: {e}"
        )

        return jsonify({
            "success": False,
            "message": "تعذر قراءة بيانات النظام"
        }), 500

    # -------------------------------------------------
    # Get OpenAI settings
    # -------------------------------------------------

    conn = get_db()

    try:
        settings = {
            r['key']: r['value']
            for r in conn.execute("""
                SELECT key, value
                FROM settings
                WHERE key IN (
                    'openai_api_key',
                    'openai_model'
                )
            """).fetchall()
        }
    finally:
        conn.close()

    api_key = (
        settings.get('openai_api_key') or ''
    ).strip()

    model = (
        settings.get('openai_model')
        or 'gpt-5.6-luna'
    ).strip()

    # -------------------------------------------------
    # If API key doesn't exist
    # -------------------------------------------------

    if not api_key:

        system = context["system"]

        return jsonify({
            "success": True,
            "answer": (
                "📊 حالة النظام اليوم\n\n"
                f"👨‍🎓 إجمالي الطلاب: {system['total_active_students']}\n"
                f"🟢 الحاضرون: {system['present_today']}\n"
                f"🔴 المتغيبون: {system['absent_today']}\n\n"
                "⚠️ المساعد الذكي الكامل غير مفعل.\n"
                "أضف OpenAI API Key من الإعدادات."
            )
        })

    # -------------------------------------------------
    # AI instructions
    # -------------------------------------------------

    import json

    system_instruction = """
أنت مساعد إداري ذكي داخل نظام حضور وإدارة مركز تعليمي.

مهمتك الإجابة على أسئلة الإدارة اعتماداً على بيانات قاعدة البيانات
المرفقة فقط.

قواعد مهمة جداً:

1. لا تخترع أي طالب أو رقم أو مبلغ.
2. لا تخمن بيانات غير موجودة.
3. إذا كانت البيانات غير موجودة قل بوضوح إنها غير موجودة.
4. أجب باللغة العربية.
5. استخدم أسماء الطلاب كما هي في قاعدة البيانات.
6. عند السؤال عن الحضور استخدم attendance الفعلي.
7. عند السؤال عن الغياب احسب الطلاب الذين لم يسجل لهم حضور في التاريخ المطلوب.
8. عند السؤال عن الشهر استخدم monthly_attendance.
9. عند السؤال عن مجموعة استخدم groups و group_attendance_today.
10. عند السؤال عن الأموال استخدم finance.
11. عند السؤال عن الطلاب الذين لم يدفعوا استخدم unpaid_students.
12. إذا تم العثور على طالب في student_matches استخدم بياناته.
13. لا تعرض معلومات حساسة لا علاقة لها بالسؤال.
14. اجعل الإجابة واضحة ومختصرة ومنظمة.
15. إذا كان السؤال يحتاج حساباً، قم بالحساب من البيانات المرفقة.

أمثلة:

"مين غاب النهارده؟"
→ قارن الطلاب النشطين مع today_attendance.

"محمد أحمد حضر كام مرة الشهر ده؟"
→ ابحث عن محمد أحمد في student_matches و student_attendance_history.

"مين أكتر طالب غياباً؟"
→ استخدم monthly_attendance وقارن عدد مرات الحضور.

"كام طالب في مجموعة السبت؟"
→ استخدم groups.

"مين عليه فلوس؟"
→ استخدم unpaid_students.

"إيرادات الشهر كام؟"
→ استخدم finance.revenue.

لا تقل إنك لا تستطيع الوصول إلى قاعدة البيانات،
لأن البيانات المطلوبة موجودة في السياق المرسل إليك.
"""

    user_payload = {
        "question": question,
        "database": context
    }

    prompt = (
        system_instruction
        + "\n\n"
        + json.dumps(
            user_payload,
            ensure_ascii=False,
            default=str
        )
    )

    # -------------------------------------------------
    # OpenAI Responses API
    # -------------------------------------------------

    try:

        response = requests.post(
            "https://api.openai.com/v1/responses",

            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            },

            json={
                "model": model,
                "input": prompt
            },

            timeout=(10, 60)
        )

        if not response.ok:

            app.logger.error(
                f"OpenAI error: {response.status_code} "
                f"{response.text[:1000]}"
            )

            return jsonify({
                "success": False,
                "message": "تعذر الاتصال بخدمة الذكاء الاصطناعي"
            }), 502

        result = response.json()

        # Preferred Responses API output
        answer = (
            result.get("output_text")
            or ""
        ).strip()

        # Fallback parser
        if not answer:

            parts = []

            for item in result.get("output", []):

                for content in item.get(
                    "content", []
                ):

                    if content.get(
                        "type"
                    ) == "output_text":

                        text = content.get(
                            "text",
                            ""
                        )

                        if text:
                            parts.append(text)

            answer = "\n".join(parts).strip()

        if not answer:

            answer = "لم يصل رد من المساعد."

        return jsonify({
            "success": True,
            "answer": answer
        })

    except requests.RequestException as e:

        app.logger.error(
            f"OpenAI request error: {e}"
        )

        return jsonify({
            "success": False,
            "message": "تعذر الاتصال بالذكاء الاصطناعي"
        }), 502

    except Exception as e:

        app.logger.error(
            f"AI assistant error: {e}"
        )

        return jsonify({
            "success": False,
            "message": "حدث خطأ أثناء تشغيل المساعد"
        }), 500
# =========================================================
# AUTOMATIC ABSENCE NOTIFICATIONS
# =========================================================
def _normalize_day(value):
    value = str(value or '').strip().lower()
    aliases = {
        'السبت':'السبت','sat':'السبت','saturday':'السبت',
        'الأحد':'الأحد','الاحد':'الأحد','sun':'الأحد','sunday':'الأحد',
        'الاثنين':'الاثنين','الإثنين':'الاثنين','mon':'الاثنين','monday':'الاثنين',
        'الثلاثاء':'الثلاثاء','tue':'الثلاثاء','tuesday':'الثلاثاء',
        'الأربعاء':'الأربعاء','الاربعاء':'الأربعاء','wed':'الأربعاء','wednesday':'الأربعاء',
        'الخميس':'الخميس','thu':'الخميس','thursday':'الخميس',
        'الجمعة':'الجمعة','fri':'الجمعة','friday':'الجمعة'
    }
    return aliases.get(value, value)


def absence_notification_worker():
    while True:
        conn = None
        try:
            now = datetime.now()
            today = now.strftime('%Y-%m-%d')
            weekday = ['الاثنين','الثلاثاء','الأربعاء','الخميس','الجمعة','السبت','الأحد'][now.weekday()]
            conn = get_db()
            groups = conn.execute('SELECT id,name,day,time FROM groups').fetchall()
            for group in groups:
                if _normalize_day(group['day']) != weekday:
                    continue
                raw_time = str(group['time'] or '').strip()
                if not raw_time:
                    continue
                class_time = None
                for fmt in ('%H:%M','%H:%M:%S'):
                    try:
                        class_time = datetime.strptime(raw_time, fmt).time()
                        break
                    except ValueError:
                        pass
                if not class_time:
                    continue
                if now < datetime.combine(now.date(), class_time) + timedelta(minutes=60):
                    continue
                students = conn.execute('''
                    SELECT s.student_id,s.name,s.telegram_chat_id
                    FROM student_groups sg JOIN students s ON s.student_id=sg.student_id
                    WHERE sg.group_id=? AND s.active_status=1
                ''',(group['id'],)).fetchall()
                for student in students:
                    if not student['telegram_chat_id']:
                        continue
                    present = conn.execute("SELECT 1 FROM attendance WHERE student_id=? AND date=? AND status='حاضر' LIMIT 1",(student['student_id'],today)).fetchone()
                    if present:
                        continue
                    already = conn.execute('''
                        SELECT 1 FROM notifications_log
                        WHERE student_id=? AND channel='telegram' AND notification_type='absence' AND created_at LIKE ? LIMIT 1
                    ''',(student['student_id'],today+'%')).fetchone()
                    if already:
                        continue
                    message=(
                        '🔔 تنبيه غياب\n━━━━━━━━━━━━━━━━\n'
                        f"👨‍🎓 الطالب: {student['name']}\n"
                        f"📅 التاريخ: {today}\n"
                        f"🏫 المجموعة: {group['name']}\n\n"
                        '⚠️ لم يتم تسجيل حضور الطالب بعد مرور ساعة على موعد الحصة.\n\n'
                        'إذا كان الطالب حاضرًا بالفعل، يرجى التواصل مع إدارة المركز.\n'
                        '━━━━━━━━━━━━━━━━'
                    )
                    conn.execute('''
                        INSERT INTO notifications_log (student_id,channel,notification_type,message,status,created_at)
                        VALUES (?, 'telegram', 'absence', ?, 'pending', ?)
                    ''',(student['student_id'],message,datetime.now().isoformat()))
            conn.commit()
        except Exception as e:
            app.logger.error(f'Absence worker error: {e}')
        finally:
            try:
                if conn: conn.close()
            except Exception: pass
        time.sleep(60)


# NOTE: this catch-all static-file route MUST stay registered after all of the
# /api/* routes above so that specific literal API paths are always matched first.
@app.get("/<path:filename>")
def serve_static(filename):
    return send_from_directory(static_dir, filename)

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
    # Create/upgrade the database BEFORE starting any background worker.
    create_tables()

    workers = [
        (telegram_notification_worker, 'TelegramNotificationWorker'),
        (whatsapp_notification_worker, 'WhatsAppNotificationWorker'),
        (telegram_polling_worker, 'TelegramPollingWorker'),
        (backup_worker, 'BackupWorker'),
        (absence_notification_worker, 'AbsenceNotificationWorker'),
    ]

    for target, name in workers:
        threading.Thread(
            target=target,
            daemon=True,
            name=name
        ).start()

    from waitress import serve
    print(f"🚀 System is running on http://0.0.0.0:{PORT}")
    serve(app, host='0.0.0.0', port=PORT, threads=8)
# =========================================================
# EXCEL REPORT EXPORT
# =========================================================

@app.get("/api/reports/<report_type>/excel")
@require_role('admin')
def export_excel_report(report_type):
    import openpyxl
    import calendar
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    from io import BytesIO

    month = request.args.get(
        'month',
        datetime.now().strftime('%Y-%m')
    )

    conn = get_db()

    try:
        wb = Workbook()
        ws = wb.active

        # -------------------------------------------------
        # إعداد الشكل العام
        # -------------------------------------------------

        ws.sheet_view.rightToLeft = True
        ws.freeze_panes = "A4"

        title_fill = PatternFill(
            "solid",
            fgColor="FFC107"
        )

        header_fill = PatternFill(
            "solid",
            fgColor="1F2937"
        )

        header_font = Font(
            bold=True,
            color="FFFFFF"
        )

        title_font = Font(
            bold=True,
            size=18
        )

        thin_border = Border(
            left=Side(style="thin", color="DDDDDD"),
            right=Side(style="thin", color="DDDDDD"),
            top=Side(style="thin", color="DDDDDD"),
            bottom=Side(style="thin", color="DDDDDD")
        )

        # -------------------------------------------------
        # حساب عدد الحصص المتوقعة للمجموعة حتى اليوم (لنسبة الحضور)
        # -------------------------------------------------

        year_num, month_num = map(int, month.split('-'))
        days_in_month = calendar.monthrange(year_num, month_num)[1]
        today_date = datetime.now().date()
        weekday_map = {
            'الاثنين': 0, 'الثلاثاء': 1, 'الأربعاء': 2, 'الخميس': 3,
            'الجمعة': 4, 'السبت': 5, 'الأحد': 6
        }

        def _expected_sessions(day_name):
            wd = weekday_map.get(str(day_name or '').strip())
            if wd is None:
                return 0
            count = 0
            for d in range(1, days_in_month + 1):
                date_obj = datetime(year_num, month_num, d).date()
                if date_obj > today_date:
                    break
                if date_obj.weekday() == wd:
                    count += 1
            return count

        # -------------------------------------------------
        # تقرير الحضور
        # -------------------------------------------------

        if report_type == "attendance":

            ws.title = "تقرير الحضور"

            ws.merge_cells("A1:F1")
            ws["A1"] = f"تقرير حضور الطلاب - {month}"
            ws["A1"].font = title_font
            ws["A1"].fill = title_fill
            ws["A1"].alignment = Alignment(
                horizontal="center"
            )

            headers = [
                "كود الطالب",
                "اسم الطالب",
                "المستوى",
                "المجموعة",
                "عدد مرات الحضور",
                "نسبة الحضور"
            ]

            for col, header in enumerate(headers, 1):
                cell = ws.cell(row=3, column=col)
                cell.value = header
                cell.fill = header_fill
                cell.font = header_font
                cell.alignment = Alignment(
                    horizontal="center"
                )
                cell.border = thin_border

            rows = conn.execute("""
                SELECT
                    s.student_id,
                    s.name,
                    s.level,
                    s.group_name,
                    g.day AS group_day,
                    COUNT(a.id) AS attendance_count
                FROM students s
                LEFT JOIN groups g
                    ON g.name = s.group_name
                LEFT JOIN attendance a
                    ON a.student_id = s.student_id
                    AND a.date LIKE ?
                GROUP BY
                    s.student_id,
                    s.name,
                    s.level,
                    s.group_name
                ORDER BY s.name
            """, (f"{month}%",)).fetchall()

            for row_idx, r in enumerate(rows, 4):

                total_sessions = _expected_sessions(r["group_day"])
                if total_sessions:
                    percentage = f"{round((r['attendance_count'] / total_sessions) * 100)}%"
                else:
                    percentage = "—"

                values = [
                    r["student_id"],
                    r["name"],
                    r["level"],
                    r["group_name"],
                    r["attendance_count"],
                    percentage
                ]

                for col_idx, value in enumerate(values, 1):
                    cell = ws.cell(
                        row=row_idx,
                        column=col_idx,
                        value=value
                    )
                    cell.border = thin_border
                    cell.alignment = Alignment(
                        horizontal="center"
                    )

        # -------------------------------------------------
        # تقرير المصروفات والإيرادات
        # -------------------------------------------------

        elif report_type == "finance":

            ws.title = "التقرير المالي"

            ws.merge_cells("A1:D1")
            ws["A1"] = f"التقرير المالي - {month}"
            ws["A1"].font = title_font
            ws["A1"].fill = title_fill
            ws["A1"].alignment = Alignment(
                horizontal="center"
            )

            headers = [
                "النوع",
                "التصنيف",
                "المبلغ",
                "التاريخ"
            ]

            for col, header in enumerate(headers, 1):
                cell = ws.cell(row=3, column=col)
                cell.value = header
                cell.fill = header_fill
                cell.font = header_font
                cell.alignment = Alignment(
                    horizontal="center"
                )
                cell.border = thin_border

            rows = conn.execute("""
                SELECT type, category, amount, date
                FROM transactions
                WHERE date LIKE ?
                ORDER BY date DESC
            """, (f"{month}%",)).fetchall()

            for row_idx, r in enumerate(rows, 4):

                values = [
                    r["type"],
                    r["category"],
                    r["amount"],
                    r["date"]
                ]

                for col_idx, value in enumerate(values, 1):
                    cell = ws.cell(
                        row=row_idx,
                        column=col_idx,
                        value=value
                    )
                    cell.border = thin_border
                    cell.alignment = Alignment(
                        horizontal="center"
                    )

        else:
            return jsonify({
                "success": False,
                "message": "نوع التقرير غير معروف"
            }), 400

        # -------------------------------------------------
        # ضبط عرض الأعمدة
        # -------------------------------------------------

        for column_cells in ws.columns:

            max_length = 0
            column_letter = get_column_letter(
                column_cells[0].column
            )

            for cell in column_cells:

                try:
                    value_length = len(
                        str(cell.value or "")
                    )

                    if value_length > max_length:
                        max_length = value_length

                except Exception:
                    pass

            ws.column_dimensions[
                column_letter
            ].width = min(max_length + 4, 35)

        # -------------------------------------------------
        # إخراج الملف
        # -------------------------------------------------

        output = BytesIO()

        wb.save(output)
        output.seek(0)

        filename = (
            f"{report_type}_report_{month}.xlsx"
        )

        return send_file(
            output,
            mimetype=(
                "application/vnd.openxmlformats-officedocument."
                "spreadsheetml.sheet"
            ),
            as_attachment=True,
            download_name=filename
        )

    except Exception as e:

        app.logger.error(
            f"Excel export error: {e}"
        )

        return jsonify({
            "success": False,
            "message": f"فشل إنشاء ملف Excel: {str(e)}"
        }), 500

    finally:
        conn.close()
        
# =========================================================
# START APPLICATION
# =========================================================

if __name__ == "__main__":
    multiprocessing.freeze_support()
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