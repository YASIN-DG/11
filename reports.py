
import io
import arabic_reshaper
from bidi.algorithm import get_display
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, Border, Side
from datetime import datetime

# Register Arabic Font
try:
    pdfmetrics.registerFont(TTFont('Arial', 'C:/Windows/Fonts/arial.ttf'))
except:
    pass

def prepare_arabic(text):
    if not text: return ""
    return get_display(arabic_reshaper.reshape(str(text)))

def create_group_pdf(group_data, students):
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    c.setFont("Arial", 12)
    
    # Title
    c.drawRightString(550, 800, prepare_arabic(f"تقرير المجموعة: {group_data['name']}"))
    c.drawRightString(550, 780, prepare_arabic(f"المعلم: {group_data['teacher']}"))
    
    # Student Table header (RTL)
    y = 750
    c.drawString(450, y, prepare_arabic("الاسم"))
    c.drawString(350, y, prepare_arabic("الكود"))
    c.drawString(200, y, prepare_arabic("حالة الدفع"))
    
    y -= 20
    for s in students:
        c.drawString(450, y, prepare_arabic(s['name']))
        c.drawString(350, y, prepare_arabic(s['student_id']))
        c.drawString(200, y, prepare_arabic(s['payment_status']))
        y -= 20
        
    c.save()
    buffer.seek(0)
    return buffer

def create_group_excel(group_data, students):
    wb = Workbook()
    ws = wb.active
    ws.sheet_view.rightToLeft = True
    
    headers = [prepare_arabic("الاسم"), prepare_arabic("الكود"), prepare_arabic("حالة الدفع")]
    ws.append(headers)
    
    for s in students:
        ws.append([s['name'], s['student_id'], s['payment_status']])
        
    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return buffer

def create_monthly_pdf(data):
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    c.setFont("Arial", 12)
    
    # Title
    c.drawRightString(550, 800, prepare_arabic("تقرير المركز الشهري"))
    
    # Summary
    y = 750
    c.drawString(450, y, prepare_arabic(f"إجمالي الطلاب: {data['total_students']}"))
    c.drawString(450, y-20, prepare_arabic(f"إجمالي سجلات الحضور: {data['total_attendance']}"))
    c.drawString(450, y-40, prepare_arabic(f"صافي الربح: {data['net_profit']}"))
    
    c.save()
    buffer.seek(0)
    return buffer

def create_monthly_excel(data):
    wb = Workbook()
    ws = wb.active
    ws.sheet_view.rightToLeft = True
    
    ws.append([prepare_arabic("الإجمالي"), prepare_arabic("القيمة")])
    ws.append([prepare_arabic("إجمالي الطلاب"), data['total_students']])
    ws.append([prepare_arabic("إجمالي سجلات الحضور"), data['total_attendance']])
    ws.append([prepare_arabic("صافي الربح"), data['net_profit']])
        
    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return buffer
