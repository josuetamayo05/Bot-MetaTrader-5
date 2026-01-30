import sqlite3

DB_PATH = "data/eurusd.sqlite"

con = sqlite3.connect(DB_PATH)

cur = con.execute("DELETE FROM alerts_once WHERE alert_key LIKE 'REPORT_SENT_%'")
con.commit()

print(f"REPORT_SENT borrados: {cur.rowcount}")
con.close()