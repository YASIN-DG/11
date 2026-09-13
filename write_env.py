import os

with open(".env", "w", encoding="utf-8") as f:
    f.write("TELEGRAM_BOT_TOKEN=8416921750:AAEuyYKDArHt_ooUvMpBLrzHMoYpGQHws6M\n")
    f.write("FLASK_SECRET_KEY=super_secret_key_for_dev\n")

print("Created .env successfully!")
