import os
import shutil

# مسار المشروع الرئيسي
project_dir = r"C:\Users\Marmosh 002\Desktop\000\attendance-system"
templates_dir = os.path.join(project_dir, "templates")
static_dir = os.path.join(project_dir, "static")

# التأكد من إنشاء المجلدات إذا لم تكن موجودة
os.makedirs(templates_dir, exist_ok=True)
os.makedirs(static_dir, exist_ok=True)

# المجلد الذي سيتم البحث فيه (نفس المجلد الرئيسي ومجلداته الفرعية)
search_base = project_dir

print("جاري البحث عن الملفات ونقلها...")
count_html = 0
count_static = 0

for root, dirs, files in os.walk(search_base):
    # تجنب البحث داخل مجلدات التجميع أو المجلدات المستهدفة نفسها
    if "templates" in root or "static" in root or "build" in root or "dist" in root or ".git" in root:
        continue
        
    for file in files:
        file_path = os.path.join(root, file)
        
        # إذا كان الملف HTML نقله إلى templates
        if file.endswith('.html'):
            target_path = os.path.join(templates_dir, file)
            if not os.path.exists(target_path):
                shutil.copy(file_path, templates_dir)
                print(f"تم نسخ ملف HTML: {file} -> templates/")
                count_html += 1
                
        # إذا كان الملف CSS, JS, أو صور نقله إلى static
        elif file.endswith(('.css', '.js', '.png', '.jpg', '.jpeg', '.ico', '.svg')):
            target_path = os.path.join(static_dir, file)
            if not os.path.exists(target_path):
                shutil.copy(file_path, static_dir)
                print(f"تم نسخ ملف تنسيق/صورة: {file} -> static/")
                count_static += 1

print(f"\nانتهى بنجاح! تم العثور على ونقل {count_html} ملف HTML و {count_static} ملف تصميم/صور.")