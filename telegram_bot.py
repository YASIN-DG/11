import sqlite3
import requests
from flask import Blueprint, request, jsonify
from datetime import datetime

telegram_bp = Blueprint("telegram_bp", __name__)

DB_PATH = "attendance.db"


# =========================================================
# DATABASE
# =========================================================

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# =========================================================
# TELEGRAM TOKEN
# =========================================================

def get_telegram_token():
    conn = get_db()

    try:
        row = conn.execute("""
            SELECT value
            FROM settings
            WHERE key = 'telegram_bot_token'
        """).fetchone()

        if row:
            return row["value"]

        return None

    finally:
        conn.close()


# =========================================================
# SEND TELEGRAM
# =========================================================

def send_telegram_message(chat_id, text):

    token = get_telegram_token()

    if not token:
        print("Telegram token not configured")
        return False

    if not chat_id:
        print("Telegram chat_id not configured")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    try:

        response = requests.post(
            url,
            json={
                "chat_id": str(chat_id),
                "text": text
            },
            timeout=10
        )

        if response.ok:
            return True

        print(
            "Telegram API error:",
            response.text
        )

        return False

    except Exception as e:

        print(
            "Telegram connection error:",
            e
        )

        return False


# =========================================================
# GET STUDENT BY TELEGRAM
# =========================================================

def get_student_by_chat_id(chat_id):

    conn = get_db()

    try:

        return conn.execute("""
            SELECT *
            FROM students
            WHERE telegram_chat_id = ?
            LIMIT 1
        """, (
            str(chat_id),
        )).fetchone()

    finally:

        conn.close()


# =========================================================
# TODAY ATTENDANCE
# =========================================================

def get_today_attendance(student_id):

    today = datetime.now().strftime("%Y-%m-%d")

    conn = get_db()

    try:

        return conn.execute("""
            SELECT *
            FROM attendance
            WHERE student_id = ?
              AND date = ?
            ORDER BY time DESC
            LIMIT 1
        """, (
            student_id,
            today
        )).fetchone()

    finally:

        conn.close()


# =========================================================
# MONTH ATTENDANCE
# =========================================================

def get_month_attendance(student_id):

    month = datetime.now().strftime("%Y-%m")

    conn = get_db()

    try:

        return conn.execute("""
            SELECT *
            FROM attendance
            WHERE student_id = ?
              AND date LIKE ?
            ORDER BY date DESC, time DESC
        """, (
            student_id,
            f"{month}%"
        )).fetchall()

    finally:

        conn.close()


# =========================================================
# STUDENT INFO
# =========================================================

def student_info(student):

    return (
        "👨‍🎓 بيانات الطالب\n"
        "━━━━━━━━━━━━━━━━\n"
        f"الاسم: {student['name']}\n"
        f"كود الطالب: {student['student_id']}\n"
        f"المستوى: {student['level'] or 'غير محدد'}\n"
        f"المجموعة: {student['group_name'] or 'غير محددة'}\n"
        f"ولي الأمر: {student['parent_name'] or 'غير محدد'}\n"
        "━━━━━━━━━━━━━━━━"
    )


# =========================================================
# TODAY ATTENDANCE MESSAGE
# =========================================================

def today_message(student):

    attendance = get_today_attendance(
        student["student_id"]
    )

    if attendance:

        return (
            "📋 حالة الحضور اليوم\n"
            "━━━━━━━━━━━━━━━━\n"
            f"👨‍🎓 الطالب: {student['name']}\n"
            f"📅 التاريخ: {attendance['date']}\n"
            f"🟢 الحالة: {attendance['status']}\n"
            f"🕐 وقت التسجيل: {attendance['time']}\n"
            "━━━━━━━━━━━━━━━━\n"
            "شكراً لتواصلكم معنا 🌷"
        )

    return (
        "📋 حالة الحضور اليوم\n"
        "━━━━━━━━━━━━━━━━\n"
        f"👨‍🎓 الطالب: {student['name']}\n"
        "🔴 لم يتم تسجيل حضور الطالب اليوم.\n"
        "━━━━━━━━━━━━━━━━"
    )


# =========================================================
# MONTH MESSAGE
# =========================================================

def month_message(student):

    rows = get_month_attendance(
        student["student_id"]
    )

    if not rows:

        return (
            f"📊 تقرير حضور {student['name']}\n\n"
            "لا توجد سجلات حضور هذا الشهر."
        )

    present = 0

    for row in rows:

        if str(row["status"]).strip() == "حاضر":
            present += 1

    message = (
        "📊 تقرير الحضور الشهري\n"
        "━━━━━━━━━━━━━━━━\n"
        f"👨‍🎓 الطالب: {student['name']}\n"
        f"📅 الشهر: {datetime.now().strftime('%Y-%m')}\n"
        f"🟢 مرات الحضور: {present}\n"
        f"📌 إجمالي السجلات: {len(rows)}\n"
        "━━━━━━━━━━━━━━━━\n\n"
        "آخر السجلات:\n"
    )

    for row in rows[:10]:

        message += (
            f"📅 {row['date']} | "
            f"🕐 {row['time']} | "
            f"{row['status']}\n"
        )

    return message


# =========================================================
# HELP
# =========================================================

def help_message(student=None):

    if student:

        greeting = (
            f"أهلاً بك 👋\n"
            f"حسابك مرتبط بالطالب: {student['name']}\n\n"
        )

    else:

        greeting = "أهلاً بك 👋\n\n"

    return (
        greeting +
        "🤖 مساعد مركز الحضور الذكي\n"
        "━━━━━━━━━━━━━━━━\n"
        "الخدمات المتاحة:\n\n"
        "📋 /حضور\n"
        "معرفة حالة الحضور اليوم.\n\n"
        "📊 /الشهر\n"
        "تقرير حضور الشهر الحالي.\n\n"
        "👨‍🎓 /بياناتي\n"
        "بيانات الطالب المرتبط.\n\n"
        "❓ /مساعدة\n"
        "عرض قائمة الخدمات.\n"
        "━━━━━━━━━━━━━━━━"
    )


# =========================================================
# WEBHOOK
# =========================================================

@telegram_bp.route(
    "/api/telegram/webhook",
    methods=["POST"]
)
def telegram_webhook():

    data = request.get_json(
        silent=True
    )

    if not data:
        return jsonify({
            "status": "ok"
        })

    message = data.get("message")

    if not message:
        return jsonify({
            "status": "ok"
        })

    chat = message.get(
        "chat",
        {}
    )

    chat_id = chat.get("id")

    if not chat_id:
        return jsonify({
            "status": "ok"
        })

    text = (
        message.get("text") or ""
    ).strip()

    if not text:
        return jsonify({
            "status": "ok"
        })


    # =====================================================
    # FIND LINKED STUDENT
    # =====================================================

    student = get_student_by_chat_id(
        chat_id
    )


    # =====================================================
    # START
    # =====================================================

    if text.lower() in [
        "/start",
        "start",
        "ابدأ"
    ]:

        if student:

            reply = (
                "👋 أهلاً وسهلاً بكم\n\n"
                f"تم ربط الحساب بالطالب:\n"
                f"👨‍🎓 {student['name']}\n\n"
                "يمكنكم الآن متابعة الحضور والبيانات.\n\n"
                "اكتب /مساعدة لرؤية الخدمات."
            )

        else:

            reply = (
                "👋 أهلاً وسهلاً بكم\n\n"
                "هذا هو مساعد مركز الحضور الذكي.\n\n"
                "لربط حساب ولي الأمر بالطالب، "
                "أرسل كود الطالب الخاص به."
            )

        send_telegram_message(
            chat_id,
            reply
        )

        return jsonify({
            "status": "ok"
        })


    # =====================================================
    # LINK STUDENT
    # =====================================================

    if not student:

        conn = get_db()

        try:

            target = conn.execute("""
                SELECT *
                FROM students
                WHERE student_id = ?
                LIMIT 1
            """, (
                text,
            )).fetchone()


            if target:

                # منع نقل الطالب لحساب Telegram آخر
                old_link = target["telegram_chat_id"]

                if old_link and str(old_link) != str(chat_id):

                    reply = (
                        "⚠️ هذا الطالب مرتبط بالفعل "
                        "بحساب Telegram آخر.\n\n"
                        "يرجى التواصل مع إدارة المركز."
                    )

                else:

                    conn.execute("""
                        UPDATE students
                        SET telegram_chat_id = ?
                        WHERE student_id = ?
                    """, (
                        str(chat_id),
                        text
                    ))

                    conn.commit()

                    reply = (
                        "✅ تم ربط الحساب بنجاح\n"
                        "━━━━━━━━━━━━━━━━\n"
                        f"👨‍🎓 الطالب: {target['name']}\n"
                        f"🆔 الكود: {target['student_id']}\n\n"
                        "من الآن سيتم إرسال إشعارات "
                        "الحضور والغياب لهذا الحساب.\n\n"
                        "اكتب /مساعدة لمعرفة الخدمات."
                    )

            else:

                reply = (
                    "❌ كود الطالب غير صحيح.\n\n"
                    "برجاء إرسال كود الطالب "
                    "المسجل في النظام."
                )

        finally:

            conn.close()

        send_telegram_message(
            chat_id,
            reply
        )

        return jsonify({
            "status": "ok"
        })


    # =====================================================
    # HELP
    # =====================================================

    if text.lower() in [
        "/مساعدة",
        "/help",
        "مساعدة"
    ]:

        send_telegram_message(
            chat_id,
            help_message(student)
        )

        return jsonify({
            "status": "ok"
        })


    # =====================================================
    # TODAY
    # =====================================================

    if text.lower() in [
        "/حضور",
        "حضور",
        "/attendance"
    ]:

        send_telegram_message(
            chat_id,
            today_message(student)
        )

        return jsonify({
            "status": "ok"
        })


    # =====================================================
    # MONTH
    # =====================================================

    if text.lower() in [
        "/الشهر",
        "الشهر",
        "/month"
    ]:

        send_telegram_message(
            chat_id,
            month_message(student)
        )

        return jsonify({
            "status": "ok"
        })


    # =====================================================
    # INFO
    # =====================================================

    if text.lower() in [
        "/بياناتي",
        "بياناتي",
        "/info"
    ]:

        send_telegram_message(
            chat_id,
            student_info(student)
        )

        return jsonify({
            "status": "ok"
        })


    # =====================================================
    # NORMAL MESSAGE
    # =====================================================

    send_telegram_message(
        chat_id,
        (
            f"أهلاً بك 👋\n\n"
            f"حسابك مرتبط بالطالب:\n"
            f"👨‍🎓 {student['name']}\n\n"
            "يمكنني مساعدتك في معرفة:\n"
            "📋 حضور اليوم\n"
            "📊 تقرير الشهر\n"
            "👨‍🎓 بيانات الطالب\n\n"
            "اكتب /مساعدة لرؤية الخدمات."
        )
    )

    return jsonify({
        "status": "ok"
    })