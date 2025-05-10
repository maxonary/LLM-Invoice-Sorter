import webbrowser
import sqlite3
import pandas as pd

# Load the CSV
df = pd.read_csv("/.csv")  # Adjust if needed

# Connect to or create SQLite DB
conn = sqlite3.connect("review_progress.db")
cursor = conn.cursor()

# Create table to store reviewed links
cursor.execute("""
    CREATE TABLE IF NOT EXISTS reviewed (
        email_link TEXT PRIMARY KEY
    )
""")
conn.commit()

# Iterate over the DataFrame
for index, row in df.iterrows():
    email_link = row["Email Link"]

    # Skip already reviewed links
    cursor.execute("SELECT 1 FROM reviewed WHERE email_link = ?", (email_link,))
    if cursor.fetchone():
        continue

    # Open the link in browser
    print(f"\nOpening email {index + 1}/{len(df)}:\n{email_link}")
    webbrowser.open(email_link)

    input("Press Enter after reviewing this email...")

    # Mark as reviewed
    cursor.execute("INSERT INTO reviewed (email_link) VALUES (?)", (email_link,))
    conn.commit()

print("✅ All emails reviewed!")

# Clean up
conn.close()