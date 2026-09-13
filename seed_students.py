"""
Run this once to load your student roster into the local database.

    python seed_students.py
"""

import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "attendance.db")

STUDENTS = [
    ("ST001", "مالك فرج الشرقاوي", "prep 1"),
    ("ST002", "حمزة عمرو", "prep 1"),
    ("ST003", "جودي عصام", "prep 1"),
    ("ST004", "براء", "prep 1"),
    ("ST005", "روزانا محمد", "prep 1"),
    ("ST006", "حبيبة أحمد", "prep 1"),
    ("ST007", "رودينا محمد", "prep 1"),
    ("ST008", "شمس محمد", "prep 1"),
    ("ST009", "عبدالحليم علي", "prep 1"),
    ("ST0010", "روضه محمد", "prep 1"),
    ("ST0011", "تيم", "prep 1"),
    ("ST0012", "عبدالرحمن حمدان", "prep 1"),
    ("ST0013", "مريم أحمد منصور", ""),
    ("ST0014", "مازن تامر", ""),
    ("ST0015", "نور محمد عربي", ""),
    ("ST0016", "مريم هشام", ""),
    ("ST0017", "رغد السعيد", ""),
    ("ST0018", "أحمد محمد", "prep 1"),
    ("ST0019", "أنس محمد", ""),
    ("ST0020", "كندة كريم", ""),
    ("ST0021", "سيرين كريم", ""),
    ("ST0022", "يونس محمد راشد", "grade 1"),
    ("ST0023", "سحر", ""),
    ("ST0024", "زينة", ""),
    ("ST0025", "جميلة", ""),
    ("KEMO", "كريم محمد صلاح", ""),
    ("ADAM", "آدم محمد صلاح", "prep 1"),
    ("ST0028", "روفانا", ""),
    ("ST0029", "فريدة", ""),
    ("ST0030", "شريف", ""),
    ("ST0031", "منة محمد علي", ""),
    ("ST0032", "حمزة", ""),
    ("ST0033", "روفان حسن", "prep 1"),
    ("ST0034", "عمرو خالد", ""),
    ("ST0035", "رنا خالد", ""),
    ("ST0036", "دارين", ""),
    ("ST0037", "محمد وائل", ""),
    ("ST0038", "أنس محمد عادل", ""),
    ("ST0039", "آيتن", ""),
    ("ST0040", "صبي", ""),
    ("ST0041", "سجي", ""),
    ("ST0042", "سنا مصطفى", ""),
    ("ST0043", "سيلا", ""),
    ("ST0044", "عائشة مصطفى", ""),
    ("ST0045", "ساجد", ""),
    ("ST0046", "مالك أيمن", ""),
    ("ST0047", "وعد أحمد", ""),
    ("ST0048", "فيروز فرح الشرقاوي", ""),
    ("ST0049", "مريم مجدي", ""),
    ("ST0050", "لارين", ""),
    ("ST0051", "محمد أيمن", ""),
    ("ST0052", "سعيد", ""),
    ("ST0053", "روضة نصر", ""),
    ("ST0054", "باسل", "prep 1"),
    ("ST0055", "محمد عمرو", "prep 1"),
    ("ST0056", "خالد محمد شريف", "prep 1"),
    ("ST0057", "جنى محمد شريف", "prep 1"),
    ("ST0058", "جنى محسن", "prep 1"),
    ("ST0059", "رؤيه", "prep 1"),
    ("ST0060", "سماح", "prep 1"),
    ("ST0061", "مالك محمد", "prep 1"),
    ("ST0062", "اشرقت", "prep 1"),
    ("ST0063", "محمود محروس", "prep 1"),
    ("ST0064", "ريتال", "prep 1"),
    ("BODY", "عبدالرحمن مبارك", "prep 1"),
    ("ST0065", "محمد ابراهيم", "prep 1"),
    ("ST0066", "مازن محمد", "prep 1"),
    ("ST0067", "سما عمرو", "prep 1"),
    ("ST0068", "حلا عمرو", "prep 1"),
    ("ST0069", "بسمله محمد", "prep 1"),
]

conn = sqlite3.connect(DB_PATH)
conn.execute("""
    CREATE TABLE IF NOT EXISTS students (
        student_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        level TEXT
    )
""")

for student_id, name, level in STUDENTS:
    conn.execute(
        "INSERT INTO students (student_id, name, level) VALUES (?, ?, ?) "
        "ON CONFLICT(student_id) DO UPDATE SET name=excluded.name, level=excluded.level",
        (student_id.strip().upper(), name.strip(), level.strip()),
    )

conn.commit()
conn.close()
print(f"تم إضافة {len(STUDENTS)} طالب لقاعدة البيانات.")
