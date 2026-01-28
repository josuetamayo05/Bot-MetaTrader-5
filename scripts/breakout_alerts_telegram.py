import argparse
import sqlite3
import requests
import yaml
import pandas as pd
from datetime import datetime, timezone

DB_PATH="data/eurusd.sqlite"
SYMBOL="EURUSD"
TIMEFRAME="M5"
PIP_SIZE=0.0001

def send_telegram(token:str, chat_id:str, text:str):
    url=f"https://api.telegram.org/bot{token}/sendMessage"
    r=requests.post(url,json={"chat_id":chat_id,"text":text})
    r.raise_for_status()

def ensure_alerts_table(db_path: str):
    sql="""
    CREATE TABLE IF NOT EXISTS alerts_log (
        ts_utc TEXT NOT NULL,
        alert_key TEXT NOT NULL,
        message TEXT NOT NULL,
        PRIMARY KEY (ts_utc, alert_key)
    );
    """
    with sqlite3.connect(db_path) as con:
        con.execute(sql)
        con.commit()

def insert_alert_if_new(db_path:str, ts_utc:str, alert_key:str, message:str) -> bool:
    # insertar o ignorar, evita duplicados
    sql="INSERT OR IGNORE INTO alerts_log(ts_utc, alert_key, message) VALUES(?, ?, ?);"
    with sqlite3.connect(db_path) as con:
        cur=con.execute(sql, (ts_utc, alert_key, message))
        con.commit()
        return cur.rowcount==1 # true si inserto (era nueva)
    
def load_range(db_path: str, date_utc:str, start_hms:str,end_hms:str)->pd.DataFrame:
    start= f"{date_utc}T{start_hms}+00:00"
    end=f"{date_utc}T{end_hms}+00:00"

    q = """
    SELECT time_utc, open, high, low, close
    FROM candles
    WHERE symbol = ?
      AND timeframe = ?
      AND time_utc >= ?
      AND time_utc < ?
    ORDER BY time_utc ASC;
    """
    with sqlite3.connect(db_path) as con:
        df=pd.read_sql_query(q,con,params=(SYMBOL,TIMEFRAME, start, end))

    if df.empty:
        return df
    
    df["time_utc"]=pd.to_datetime(df["time_utc"],utc=True)
    return df

def find_breakouts(df_after:pd.DataFrame,hi:float,lo:float,key_prefix:str):
    alerts=[]
    broken_high=False
    broken_low=False

    for r in df_after.itertuples(index=False):
        t=r.time_utc
        c=float(r.close)

        if (not broken_low) and c<lo:
            broken_low=True
            alerts.append({
                "ts": t,
                "key": f"{key_prefix}_BROKE_LOW",
                "close": c,
                "hi": hi,
                "lo": lo,
            })

        if (not broken_high) and c>hi:
            broken_high=True
            alerts.append({
                "ts": t,
                "key": f"{key_prefix}_BROKE_HIGH",
                "close": c,
                "hi": hi,
                "lo": lo,
            })

        if broken_high and broken_low:
            break
    
    return alerts

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--date", help="UTC date YYYY-MM-DD (default: today UTC)")
    args=parser.parse_args()

    cfg=yaml.safe_load(open("src/config/settings.yaml", "r", encoding="utf-8"))
    token=cfg["telegram"]["token"]
    chat_id=float(cfg["telegram"]["chat_id"])

    date_utc=args.date or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    ensure_alerts_table(DB_PATH)

    # Asia (00:00-07:00) → monitorear 07:00-22:00
    asia=load_range(DB_PATH, date_utc, "00:00:00", "07:00:00")
    if asia.empty:
        print("No Asia data")
        return
    asia_hi=float(asia["high"].max())
    asia_lo=float(asia["low"].min())
    after_asia=load_range(DB_PATH, date_utc, "07:00:00","22:00:00")
    asia_alerts=find_breakouts(after_asia, asia_hi, asia_lo, "ASIA_RANGE")

    # London (07:00-13:00) --> monitorear 13:00-22:00
    london=load_range(DB_PATH,date_utc,"07:00:00","13:00:00")
    if london.empty:
        print("No London data.")
        return
    london_hi=float(london["high"].max())
    london_lo=float(london["low"].min())
    after_london=load_range(DB_PATH, date_utc, "07:00:00","13:00:00")
    london_alerts=find_breakouts(after_london, london_hi,london_lo,"LONDON_RANGE")

    all_alerts=asia_alerts+london_alerts
    if not all_alerts:
        print("No breakouts detected.")
        return
    
    # Enviar solo nuevas no duplicadas)
    sent=0
    for a in all_alerts:
        ts=a["ts"]
        ts_iso=ts.isoformat()
        key=a["key"]

        key_to_text = {
            "ASIA_RANGE_BROKE_LOW": "Rango ASIA: rompió el MÍNIMO",
            "ASIA_RANGE_BROKE_HIGH": "Rango ASIA: rompió el MÁXIMO",
            "LONDON_RANGE_BROKE_LOW": "Rango LONDRES: rompió el MÍNIMO",
            "LONDON_RANGE_BROKE_HIGH": "Rango LONDRES: rompió el MÁXIMO",
        }

        titulo = key_to_text.get(key, key.replace("_", " "))

        msg = (
            f"📊 EURUSD {TIMEFRAME} | {date_utc} (UTC)\n"
            f"🚨 {titulo}\n"
            f"🕒 Hora: {ts.strftime('%H:%M')}\n"
            f"📌 Cierre: {a['close']:.5f}\n"
            f"📈 Rango: {a['lo']:.5f} — {a['hi']:.5f}"
        )

        if insert_alert_if_new(DB_PATH, ts_iso, key, msg):
            send_telegram(token,chat_id, msg)
            sent+=1

    print(f"Done. Sent {sent} new alerts (duplicates skipped).")

if __name__=="__main__":
    main()
    