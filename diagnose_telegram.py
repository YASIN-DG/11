import sqlite3
import requests
import os
import sys

# Path to the database
db_path = "attendance.db"

def diagnose():
    print("--- 🔍 Telegram Diagnostic Tool 🔍 ---\n")
    
    # 1. Check Database
    if not os.path.exists(db_path):
        print(f"❌ Error: Database file '{db_path}' not found!")
        return
        
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        settings = {row[1]: row[2] for row in cursor.execute("SELECT * FROM settings").fetchall()}
        conn.close()
        
        token = settings.get("bot_token")
        enabled = settings.get("telegram_enabled")
        
        print("✅ Database connection successful.")
        print(f"   - Telegram Enabled: {enabled == '1'}")
        print(f"   - Bot Token: {'Present' if token else 'MISSING'}")
        
        if not token or enabled != '1':
            print("⚠️ Configuration seems incomplete or disabled in the database.")
    except Exception as e:
        print(f"❌ Error reading database: {e}")
        return

    # 2. Check Network Connection
    print("\n--- Testing Connectivity ---")
    if not token:
        print("❌ Cannot test API without token.")
    else:
        url = f"https://api.telegram.org/bot{token}/getMe"
        print(f"Testing URL: {url}")
        try:
            # Check for proxy settings
            proxies = {
                "http": os.environ.get("HTTP_PROXY"),
                "https": os.environ.get("HTTPS_PROXY"),
            }
            print(f"Using Proxies: {proxies}")
            
            resp = requests.get(url, timeout=15, proxies=proxies)
            
            if resp.status_code == 200:
                print("✅ Successfully connected to Telegram API!")
            else:
                print(f"❌ Connection to Telegram API failed.")
                print(f"   - HTTP Status Code: {resp.status_code}")
                print(f"   - Response Body: {resp.text}")
        except requests.exceptions.ProxyError:
            print("❌ Proxy Error: Please check your proxy settings.")
        except requests.exceptions.SSLError:
            print("❌ SSL Error: There is an issue with the SSL certificate validation (possibly an enterprise firewall/interception).")
        except requests.exceptions.ConnectTimeout:
            print("❌ Timeout: The connection to api.telegram.org timed out. Check firewall or internet connectivity.")
        except Exception as e:
            print(f"❌ An unexpected error occurred: {e}")

    print("\n--- 🏁 Diagnostic Finished 🏁 ---")

if __name__ == "__main__":
    diagnose()
