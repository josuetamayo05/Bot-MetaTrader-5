import time
import sqlite3
import yaml
import requests
import MetaTrader5 as mt5
import pandas as pd
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

DB_PATH = "data/eurusd.sqlite"
PIP_SIZE = 0.0001

SESSIONS = {
    "ASIA":   ("00:00:00", "07:00:00"),
    "LONDON": ("07:00:00", "13:00:00"),
    "NY":     ("13:00:00", "22:00:00"),
}

def send_telegram(token: str, chat_id: int, text: str):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    r = requests.post(url, json={"chat_id": chat_id, "text": text})
    r.raise_for_status()

def ensure_candles_table():
    sql = """
    CREATE TABLE IF NOT EXISTS candles (
      symbol TEXT NOT NULL,
      timeframe TEXT NOT NULL,
      time_utc TEXT NOT NULL,
      open REAL NOT NULL,
      high REAL NOT NULL,
      low REAL NOT NULL,
      close REAL NOT NULL,
      tick_volume INTEGER,
      spread INTEGER,
      real_volume INTEGER,
      PRIMARY KEY (symbol, timeframe, time_utc)
    );
    """
    Path("data").mkdir(exist_ok=True)
    with sqlite3.connect(DB_PATH) as con:
        con.execute(sql)
        con.commit()

def save_bar_to_sqlite(symbol: str, tf_name: str, bar: pd.Series) -> bool:
    sql = """
    INSERT OR IGNORE INTO candles
    (symbol, timeframe, time_utc, open, high, low, close, tick_volume, spread, real_volume)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
    """
    with sqlite3.connect(DB_PATH) as con:
        cur = con.execute(sql, (
            symbol, tf_name, bar["time_utc"].isoformat(),
            float(bar["open"]), float(bar["high"]), float(bar["low"]), float(bar["close"]),
            None, None, None
        ))
        con.commit()
        return cur.rowcount == 1

def ensure_alerts_once_table():
    sql = """
    CREATE TABLE IF NOT EXISTS alerts_once (
      alert_key TEXT PRIMARY KEY,
      first_ts_utc TEXT NOT NULL,
      message TEXT NOT NULL
    );
    """
    with sqlite3.connect(DB_PATH) as con:
        con.execute(sql)
        con.commit()

def insert_once(alert_key: str, ts_utc_iso: str, message: str) -> bool:
    sql = "INSERT OR IGNORE INTO alerts_once(alert_key, first_ts_utc, message) VALUES (?, ?, ?);"
    with sqlite3.connect(DB_PATH) as con:
        cur = con.execute(sql, (alert_key, ts_utc_iso, message))
        con.commit()
        return cur.rowcount == 1
    
def mt5_copy_rates_range_safe(symbol: str, timeframe, dt_from, dt_to):
    rates = mt5.copy_rates_range(symbol, timeframe, dt_from, dt_to)
    if rates is not None and len(rates) > 0:
        return rates
    # fallback naive datetimes
    rates = mt5.copy_rates_range(symbol, timeframe, dt_from.replace(tzinfo=None), dt_to.replace(tzinfo=None))
    return rates

def load_session_rates(symbol: str, timeframe, date_utc: str, start_hms: str, end_hms: str) -> pd.DataFrame:
    start = datetime.fromisoformat(f"{date_utc}T{start_hms}+00:00")
    end   = datetime.fromisoformat(f"{date_utc}T{end_hms}+00:00")
    rates = mt5_copy_rates_range_safe(symbol, timeframe, start, end)
    if rates is None or len(rates) == 0:
        return pd.DataFrame()
    df = pd.DataFrame(rates)
    df["time_utc"] = pd.to_datetime(df["time"], unit="s", utc=True)
    return df[["time_utc", "open", "high", "low", "close"]].sort_values("time_utc")

def get_last_closed_bar(symbol: str, timeframe) -> pd.Series | None:
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 1, 1)
    if rates is None or len(rates) == 0:
        return None
    r = rates[0]
    t = datetime.fromtimestamp(r["time"], tz=timezone.utc)
    return pd.Series({"time_utc": t, "open": r["open"], "high": r["high"], "low": r["low"], "close": r["close"]})

def get_mid_price(symbol: str) -> tuple[datetime, float] | tuple[None, None]:
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return None, None
    ts = datetime.fromtimestamp(tick.time, tz=timezone.utc)
    mid = (tick.bid + tick.ask) / 2.0
    return ts, mid

def fmt_h(ts_utc: datetime, local_tz: ZoneInfo):
    return ts_utc.astimezone(local_tz).strftime("%H:%M"), ts_utc.strftime("%H:%M")

def main():
    cfg = yaml.safe_load(open("src/config/settings.yaml", "r", encoding="utf-8"))

    symbol = cfg.get("symbol", "EURUSD")
    tf_name = cfg.get("timeframe", "M5")
    poll_seconds = int(cfg.get("poll_seconds", 10))
    trigger_mode = cfg.get("trigger_mode", "tick")
    buffer_pips = float(cfg.get("break_buffer_pips", 1.0))
    buffer_px = buffer_pips * PIP_SIZE

    token = cfg["telegram"]["token"]
    chat_id = int(cfg["telegram"]["chat_id"])
    local_tz = ZoneInfo(cfg.get("timezone", {}).get("local", "America/Havana"))

    tf_map = {"M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15, "H1": mt5.TIMEFRAME_H1}
    timeframe = tf_map[tf_name]

    ensure_candles_table()
    ensure_alerts_once_table()

    mt5_path=cfg.get("mt5_path")
    def init_mt5_with_retries():
        for attempt in range(1, 6):
            ok = mt5.initialize(path=mt5_path, timeout=60000)
            if ok:
                return True
            err = mt5.last_error()
            print(f"[MT5] init attempt {attempt}/5 failed: {err}")
            mt5.shutdown()
            time.sleep(2 * attempt)
        return False

    if not init_mt5_with_retries():
        raise RuntimeError(f"MT5 init failed after retries: {mt5.last_error()}")

    if not mt5.symbol_select(symbol, True):
        raise RuntimeError(f"symbol_select failed: {mt5.last_error()}")

    print("LIVE v2 started. CTRL+C to stop.")
    last_date_utc = None
    last_bar_time = None

    try:
        while True:
            now_utc = datetime.now(timezone.utc)
            date_utc = now_utc.strftime("%Y-%m-%d")

            # Mensaje “bot activo” al iniciar cada día UTC
            if last_date_utc != date_utc:
                last_date_utc = date_utc
                msg = f"📍 Bot activo | {symbol} {tf_name}\nDía UTC: {date_utc}\nHora Cuba: {now_utc.astimezone(local_tz).strftime('%H:%M')}"
                key = f"BOT_ACTIVE_{date_utc}"
                if insert_once(key, now_utc.isoformat(), msg):
                    send_telegram(token, chat_id, msg)
                print(f"\n--- New UTC day: {date_utc} ---")

            # Guardar vela cerrada M5 para tu DB
            bar = get_last_closed_bar(symbol, timeframe)
            if bar is not None:
                if last_bar_time is None or bar["time_utc"] > last_bar_time:
                    last_bar_time = bar["time_utc"]
                    save_bar_to_sqlite(symbol, tf_name, bar)

            # Obtener precio para disparo (tick recomendado)
            if trigger_mode == "tick":
                ts_tick, price = get_mid_price(symbol)
            else:
                ts_tick = bar["time_utc"] if bar is not None else None
                price = float(bar["close"]) if bar is not None else None

            if ts_tick is None or price is None:
                time.sleep(poll_seconds)
                continue

            # Cargar rangos completos de ASIA y LONDRES (del día actual)
            asia_df = load_session_rates(symbol, timeframe, date_utc, *SESSIONS["ASIA"])
            london_df = load_session_rates(symbol, timeframe, date_utc, *SESSIONS["LONDON"])

            # ASIA: solo alertar de 07:00 a 22:00 UTC
            asia_end = datetime.fromisoformat(f"{date_utc}T07:00:00+00:00")
            day_end = datetime.fromisoformat(f"{date_utc}T22:00:00+00:00")
            if not asia_df.empty and ts_tick >= asia_end and ts_tick < day_end:
                asia_hi = float(asia_df["high"].max())
                asia_lo = float(asia_df["low"].min())
                local_h, utc_h = fmt_h(ts_tick, local_tz)

                if price > asia_hi + buffer_px:
                    key = f"{date_utc}_ASIA_BROKE_HIGH"
                    msg = (
                        f"📊 EURUSD {tf_name}\n"
                        f"🚨 Rango ASIA: rompió el MÁXIMO (+{buffer_pips:.1f} pip)\n"
                        f"🕒 Cuba {local_h} | UTC {utc_h}\n"
                        f"📌 Precio: {price:.5f}\n"
                        f"📈 Asia: {asia_lo:.5f} — {asia_hi:.5f}"
                    )
                    if insert_once(key, ts_tick.isoformat(), msg):
                        send_telegram(token, chat_id, msg)

                if price < asia_lo - buffer_px:
                    key = f"{date_utc}_ASIA_BROKE_LOW"
                    msg = (
                        f"📊 EURUSD {tf_name}\n"
                        f"🚨 Rango ASIA: rompió el MÍNIMO (-{buffer_pips:.1f} pip)\n"
                        f"🕒 Cuba {local_h} | UTC {utc_h}\n"
                        f"📌 Precio: {price:.5f}\n"
                        f"📈 Asia: {asia_lo:.5f} — {asia_hi:.5f}"
                    )
                    if insert_once(key, ts_tick.isoformat(), msg):
                        send_telegram(token, chat_id, msg)

            # LONDRES: solo alertar de 13:00 a 22:00 UTC
            london_end = datetime.fromisoformat(f"{date_utc}T13:00:00+00:00")
            if not london_df.empty and ts_tick >= london_end and ts_tick < day_end:
                london_hi = float(london_df["high"].max())
                london_lo = float(london_df["low"].min())
                local_h, utc_h = fmt_h(ts_tick, local_tz)

                if price > london_hi + buffer_px:
                    key = f"{date_utc}_LONDON_BROKE_HIGH"
                    msg = (
                        f"📊 EURUSD {tf_name}\n"
                        f"🚨 Rango LONDRES: rompió el MÁXIMO (+{buffer_pips:.1f} pip)\n"
                        f"🕒 Cuba {local_h} | UTC {utc_h}\n"
                        f"📌 Precio: {price:.5f}\n"
                        f"📈 Londres: {london_lo:.5f} — {london_hi:.5f}"
                    )
                    if insert_once(key, ts_tick.isoformat(), msg):
                        send_telegram(token, chat_id, msg)

                if price < london_lo - buffer_px:
                    key = f"{date_utc}_LONDON_BROKE_LOW"
                    msg = (
                        f"📊 EURUSD {tf_name}\n"
                        f"🚨 Rango LONDRES: rompió el MÍNIMO (-{buffer_pips:.1f} pip)\n"
                        f"🕒 Cuba {local_h} | UTC {utc_h}\n"
                        f"📌 Precio: {price:.5f}\n"
                        f"📈 Londres: {london_lo:.5f} — {london_hi:.5f}"
                    )
                    if insert_once(key, ts_tick.isoformat(), msg):
                        send_telegram(token, chat_id, msg)

            print(f"[{ts_tick.strftime('%H:%M:%S')} UTC] mid={price:.5f} (poll {poll_seconds}s)")
            time.sleep(poll_seconds)

    except KeyboardInterrupt:
        print("Stopping...")
    finally:
        mt5.shutdown()

if __name__ == "__main__":
    main()

