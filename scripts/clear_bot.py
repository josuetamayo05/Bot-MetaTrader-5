import sqlite3

con = sqlite3.connect("data/eurusd.sqlite")
con.execute("DELETE FROM alerts_once WHERE alert_key LIKE 'BOT_ACTIVE_%'")
con.commit()
con.close()
print("BOT_ACTIVE borrados")