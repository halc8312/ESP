import sqlite3
import os

db_path = os.path.join("f:\\", "ESP-main", "ESP-main", "mercari.db")
print(f"Connecting to {db_path}")
conn = sqlite3.connect(db_path)
cursor = conn.cursor()
cursor.execute("SELECT id FROM items WHERE status='active' LIMIT 5;")
urls = cursor.fetchall()
print("Found Item IDs:")
for u in urls:
    print(f"https://jp.mercari.com/item/{u[0]}")
