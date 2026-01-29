import time
import sqlite3
import yaml
import requests
import MetaTrader5 as mt5
import pandas as pd
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

DB_PATH = "data/eurusd.sqlite"
PIP_SIZE = 0.0001

SESSIONS = {
    "ASIA":   ("00:00:00", "07:00:00"),
    "LONDON": ("07:00:00", "13:00:00"),
    "NY":     ("13:00:00", "22:00:00"),
}

def send_telegram(token: str, chat_id: int, text: str):
    url=f"https://api.telegram.org/bot{token}/sendMessage"
    r=requests.post(url, json={"chat_id": chat_id, "text": text})
    r.raise_for_status()

def ensure_alerts_table():
    sql="""
    CREATE TABLE IF NOT EXISTS alerts_log (
        ts_utc TEXT NOT NULL,
        alert_key TEXT NOT NULL,
        message TEXT NOT NULL,
        PRIMARY KEY (ts_utc, alert_key)    
    );
    """
    with sqlite3.connect(DB_PATH) as con:
        cur=con.execute(sql)
        con.commit()

def insert_alert_if_new(ts_utc_iso: str, alert_key: str, message:str)->bool:
    sql="INSERT OR IGNORE INTO alerts_log(ts_utc, alert_key, message) VALUES(?, ?, ?);"
    with sqlite3.connect(DB_PATH) as con:
        cur=con.execute(sql,(ts_utc_iso,alert_key, message))
        con.commit()
        return cur.rowcount==1
    
def mt5_copy_rates_range_safe(symbol:str, timeframe, dt_from, dt_to):
    # Algunas instalaciones prefieren datetimes "naive" (sin tzinfo).
    rates=mt5.copy_rates_range(symbol, timeframe, dt_from, dt_to)
    if rates is not None and len(rates)>0:
        return rates

    dt_from2=dt_from.replace(tzinfo=None)
    dt_to2=dt_to.replace(tzinfo=None)
    rates=mt5.copy_rates_range(symbol, timeframe, dt_from2, dt_to2)
    return rates

def load_session_rates(symbol:str,timeframe,date_utc:str,start_hms:str,end_hms:str)->pd.DataFrame:
    start=datetime.fromisoformat(f"{date_utc}T{start_hms}+00:00")
    end=datetime.fromisoformat(f"{date_utc}T{end_hms}+00:00")

    rates=mt5_copy_rates_range_safe(symbol,timeframe,start,end)
    if rates is None or len(rates)==0:
        return pd.DataFrame()
    
    df=pd.DataFrame(rates)
    df["time_utc"]=pd.to_datetime(df["time"],unit="s",utc=True)
    return df[["time_utc","open","high","low","close"]].sort_values("time_utc")


def get_last_closed_bar(symbol: str, timeframe)-> pd.Series | None: 
    #pos 1 suele ser la ultima vela cerrada, pos=0 puede estar en formacion
    rates=mt5.copy_rates_from_pos(symbol, timeframe, 1, 1)
    if rates is None or len(rates)==0:
        return None
    r=rates[0]
    t=datetime.fromtimestamp(r["time"], tz=timezone.utc)
    return pd.Series({"time_utc": t, "open": r["open"], "high": r["high"], "low": r["low"], "close": r["close"]})

def fmt_time(ts_utc:datetime, local_tz:ZoneInfo):
    return ts_utc.astimezone(local_tz).strftime("%H:%M"), ts_utc.strftime("%H:%M")

def main():
    cfg=yaml.safe_load(open("src/config/settings.yaml","r",encoding="utf-8"))
    symbol=cfg.get("symbol", "EURUSD")
    tf_name=cfg.get("timeframe","M5")
    poll_seconds=int(cfg.get("poll_seconds", 60))
    token=cfg["telegram"]["token"]
    chat_id=int(cfg["telegram"]["chat_id"])
    local_tz = ZoneInfo(cfg.get("timezone", {}).get("local", "America/Havana"))

    tf_map = {"M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15, "H1": mt5.TIMEFRAME_H1}
    timeframe = tf_map[tf_name]

    ensure_alerts_table()

    if not mt5.initialize():
        raise RuntimeError(f"MT5 init failed: {mt5.last_error()}")
    
    if not mt5.symbol_select(symbol,True):
        raise RuntimeError(f"symbol_select failed: {mt5.last_error()}")
    
    print("LIVE started. Press CTRL+C to stop.")
    last_processed_bar_time=None
    last_date_utc=None

    try:
        while True:
            now_utc = datetime.now(timezone.utc)
            date_utc = now_utc.strftime("%Y-%m-%d")

            # Si cambia el día UTC, reinicia estado local
            if last_date_utc != date_utc:
                last_date_utc = date_utc
                last_processed_bar_time = None
                print(f"\n--- New UTC day: {date_utc} ---")

            bar = get_last_closed_bar(symbol, timeframe)
            if bar is None:
                time.sleep(poll_seconds)
                continue

            bar_time = bar["time_utc"]
            if last_processed_bar_time is not None and bar_time <= last_processed_bar_time:
                time.sleep(poll_seconds)
                continue

            last_processed_bar_time = bar_time

            # Carga rangos completos (hasta donde existan) para el día actual
            asia_df = load_session_rates(symbol, timeframe, date_utc, *SESSIONS["ASIA"])
            london_df = load_session_rates(symbol, timeframe, date_utc, *SESSIONS["LONDON"])

            # Si aún no hay data suficiente, sigue
            if asia_df.empty:
                time.sleep(poll_seconds)
                continue

            asia_hi = float(asia_df["high"].max())
            asia_lo = float(asia_df["low"].min())

            c = float(bar["close"])

            # Alertas por ruptura de ASIA (solo después de 07:00 UTC)
            if bar_time >= datetime.fromisoformat(f"{date_utc}T07:00:00+00:00") and bar_time < datetime.fromisoformat(f"{date_utc}T22:00:00+00:00"):
                if c > asia_hi:
                    local_h, utc_h = fmt_time(bar_time, local_tz)
                    key = "ASIA_RANGE_BROKE_HIGH"
                    msg = (
                        f"📊 EURUSD {tf_name}\n"
                        f"🚨 Rango ASIA: rompió el MÁXIMO\n"
                        f"🕒 Hora Cuba: {local_h} | UTC: {utc_h}\n"
                        f"📌 Cierre: {c:.5f}\n"
                        f"📈 Asia: {asia_lo:.5f} — {asia_hi:.5f} ({(asia_hi-asia_lo)/PIP_SIZE:.1f} pips)"
                    )
                    if insert_alert_if_new(bar_time.isoformat(), key, msg):
                        send_telegram(token, chat_id, msg)

                if c < asia_lo:
                    local_h, utc_h = fmt_time(bar_time, local_tz)
                    key = "ASIA_RANGE_BROKE_LOW"
                    msg = (
                        f"📊 EURUSD {tf_name}\n"
                        f"🚨 Rango ASIA: rompió el MÍNIMO\n"
                        f"🕒 Hora Cuba: {local_h} | UTC: {utc_h}\n"
                        f"📌 Cierre: {c:.5f}\n"
                        f"📈 Asia: {asia_lo:.5f} — {asia_hi:.5f} ({(asia_hi-asia_lo)/PIP_SIZE:.1f} pips)"
                    )
                    if insert_alert_if_new(bar_time.isoformat(), key, msg):
                        send_telegram(token, chat_id, msg)

            # Alertas por ruptura de LONDRES (solo después de 13:00 UTC)
            if not london_df.empty and bar_time >= datetime.fromisoformat(f"{date_utc}T13:00:00+00:00") and bar_time < datetime.fromisoformat(f"{date_utc}T22:00:00+00:00"):
                london_hi = float(london_df["high"].max())
                london_lo = float(london_df["low"].min())

                if c > london_hi:
                    local_h, utc_h = fmt_time(bar_time, local_tz)
                    key = "LONDON_RANGE_BROKE_HIGH"
                    msg = (
                        f"📊 EURUSD {tf_name}\n"
                        f"🚨 Rango LONDRES: rompió el MÁXIMO\n"
                        f"🕒 Hora Cuba: {local_h} | UTC: {utc_h}\n"
                        f"📌 Cierre: {c:.5f}\n"
                        f"📈 Londres: {london_lo:.5f} — {london_hi:.5f} ({(london_hi-london_lo)/PIP_SIZE:.1f} pips)"
                    )
                    if insert_alert_if_new(bar_time.isoformat(), key, msg):
                        send_telegram(token, chat_id, msg)

                if c < london_lo:
                    local_h, utc_h = fmt_time(bar_time, local_tz)
                    key = "LONDON_RANGE_BROKE_LOW"
                    msg = (
                        f"📊 EURUSD {tf_name}\n"
                        f"🚨 Rango LONDRES: rompió el MÍNIMO\n"
                        f"🕒 Hora Cuba: {local_h} | UTC: {utc_h}\n"
                        f"📌 Cierre: {c:.5f}\n"
                        f"📈 Londres: {london_lo:.5f} — {london_hi:.5f} ({(london_hi-london_lo)/PIP_SIZE:.1f} pips)"
                    )
                    if insert_alert_if_new(bar_time.isoformat(), key, msg):
                        send_telegram(token, chat_id, msg)

            print(f"[{bar_time.strftime('%H:%M')} UTC] last close={c:.5f} (live ok)")
            time.sleep(poll_seconds)

    except KeyboardInterrupt:
        print("Stopping...")
    finally:
        mt5.shutdown()

if __name__=="__main__":
    main()
