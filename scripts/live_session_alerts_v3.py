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

# ---------------- Telegram ----------------
def send_telegram(token: str, chat_id: int, text: str):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    r = requests.post(url, json={"chat_id": chat_id, "text": text})
    r.raise_for_status()

# ---------------- DB tables ----------------
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

def exists_once(alert_key: str) -> bool:
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute("SELECT 1 FROM alerts_once WHERE alert_key=? LIMIT 1", (alert_key,)).fetchone()
    return row is not None

# ---------------- Helpers ----------------
def fmt_h(ts_utc: datetime, local_tz: ZoneInfo):
    return ts_utc.astimezone(local_tz).strftime("%H:%M"), ts_utc.strftime("%H:%M")

def mt5_copy_rates_range_safe(symbol: str, timeframe, dt_from, dt_to):
    rates = mt5.copy_rates_range(symbol, timeframe, dt_from, dt_to)
    if rates is not None and len(rates) > 0:
        return rates
    # fallback naive datetimes
    return mt5.copy_rates_range(symbol, timeframe, dt_from.replace(tzinfo=None), dt_to.replace(tzinfo=None))

def load_rates_utc(symbol: str, timeframe, start_utc: datetime, end_utc: datetime) -> pd.DataFrame:
    rates = mt5_copy_rates_range_safe(symbol, timeframe, start_utc, end_utc)
    if rates is None or len(rates) == 0:
        return pd.DataFrame()
    df = pd.DataFrame(rates)
    df["time_utc"] = pd.to_datetime(df["time"], unit="s", utc=True)
    return df[["time_utc", "open", "high", "low", "close"]].sort_values("time_utc")

def get_mid_price(symbol: str):
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return None, None
    ts = datetime.fromtimestamp(tick.time, tz=timezone.utc)
    mid = (tick.bid + tick.ask) / 2.0
    return ts, float(mid)

def get_session_bounds(date_utc: str, start_hms: str, end_hms: str):
    start = datetime.fromisoformat(f"{date_utc}T{start_hms}+00:00")
    end   = datetime.fromisoformat(f"{date_utc}T{end_hms}+00:00")
    return start, end

def get_session_percentile(session_name: str, lookback_days: int, q: float) -> float | None:
    # toma los últimos N rangos de esa sesión
    sql = """
    SELECT range_pips
    FROM session_summary
    WHERE session = ?
      AND range_pips IS NOT NULL
      AND bars > 0
    ORDER BY date_utc DESC
    LIMIT ?;
    """
    with sqlite3.connect(DB_PATH) as con:
        rows = con.execute(sql, (session_name, lookback_days)).fetchall()
    if not rows:
        return None
    s = pd.Series([float(r[0]) for r in rows])
    return float(s.quantile(q))

# ---------------- ATR ----------------
def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    # True Range
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        (df["high"] - df["low"]),
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs()
    ], axis=1).max(axis=1)
    atr = tr.rolling(period).mean()
    return atr

def atr_baseline_p90_from_db(date_utc: str, lookback_days: int = 30, period: int = 14) -> float | None:
    # usa velas M5 de los últimos N días (hasta el inicio del día actual UTC)
    start_day = datetime.fromisoformat(f"{date_utc}T00:00:00+00:00")
    start = start_day - timedelta(days=lookback_days)
    end = start_day

    q = """
    SELECT time_utc, high, low, close
    FROM candles
    WHERE symbol = ?
      AND timeframe = ?
      AND time_utc >= ?
      AND time_utc < ?
    ORDER BY time_utc ASC;
    """
    with sqlite3.connect(DB_PATH) as con:
        df = pd.read_sql_query(q, con, params=("EURUSD", "M5", start.isoformat(), end.isoformat()))
    if df.empty or len(df) < period + 5:
        return None

    df["time_utc"] = pd.to_datetime(df["time_utc"], utc=True)
    df["high"] = df["high"].astype(float)
    df["low"] = df["low"].astype(float)
    df["close"] = df["close"].astype(float)

    atr = compute_atr(df, period=period).dropna()
    if atr.empty:
        return None

    atr_pips = atr / PIP_SIZE
    return float(atr_pips.quantile(0.90))

def atr_current_from_mt5(symbol: str, timeframe, bars: int = 300, period: int = 14) -> float | None:
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, bars)
    if rates is None or len(rates) < period + 5:
        return None
    df = pd.DataFrame(rates)
    df["high"] = df["high"].astype(float)
    df["low"] = df["low"].astype(float)
    df["close"] = df["close"].astype(float)

    atr = compute_atr(df, period=period).dropna()
    if atr.empty:
        return None
    atr_last_pips = float(atr.iloc[-1] / PIP_SIZE)
    return atr_last_pips

# ---------------- Main ----------------
def main():
    cfg = yaml.safe_load(open("src/config/settings.yaml", "r", encoding="utf-8"))

    symbol = cfg.get("symbol", "EURUSD")
    tf_name = cfg.get("timeframe", "M5")
    poll_seconds = int(cfg.get("poll_seconds", 10))
    buffer_pips = float(cfg.get("break_buffer_pips", 1.0))
    buffer_px = buffer_pips * PIP_SIZE

    token = cfg["telegram"]["token"]
    chat_id = int(cfg["telegram"]["chat_id"])
    local_tz = ZoneInfo(cfg.get("timezone", {}).get("local", "America/Havana"))

    tf_map = {"M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15, "H1": mt5.TIMEFRAME_H1}
    timeframe = tf_map[tf_name]

    ensure_alerts_once_table()

    if not mt5.initialize():
        raise RuntimeError(f"MT5 init failed: {mt5.last_error()}")
    if not mt5.symbol_select(symbol, True):
        raise RuntimeError(f"symbol_select failed: {mt5.last_error()}")

    print("LIVE v3 started. CTRL+C to stop.")

    last_date_utc = None
    ny_p90 = None
    atr_p90 = None

    try:
        while True:
            now_utc = datetime.now(timezone.utc)
            date_utc = now_utc.strftime("%Y-%m-%d")

            # Nuevo día UTC: recalcular baselines (NY p90 y ATR p90)
            if last_date_utc != date_utc:
                last_date_utc = date_utc

                # p90 NY basado en session_summary (41 días en tu caso)
                ny_p90 = get_session_percentile("NY", lookback_days=60, q=0.90)

                # ATR p90 basado en velas M5 de últimos 30 días en tu DB
                atr_p90 = atr_baseline_p90_from_db(date_utc, lookback_days=30, period=14)

                msg = (
                    f"📍 Bot activo | {symbol} {tf_name}\n"
                    f"Día UTC: {date_utc}\n"
                    f"Hora Cuba: {now_utc.astimezone(local_tz).strftime('%H:%M')}\n"
                    f"Baselines: NY p90={ny_p90:.1f} pips | ATR p90={atr_p90:.1f} pips"
                    if (ny_p90 is not None and atr_p90 is not None)
                    else
                    f"📍 Bot activo | {symbol} {tf_name}\nDía UTC: {date_utc}"
                )
                key = f"BOT_ACTIVE_{date_utc}"
                if insert_once(key, now_utc.isoformat(), msg):
                    send_telegram(token, chat_id, msg)

                print(f"\n--- New UTC day: {date_utc} ---")

            # Precio actual (tick)
            ts_tick, price = get_mid_price(symbol)
            if ts_tick is None or price is None:
                time.sleep(poll_seconds)
                continue

            # Rangos de ASIA y LONDRES “completos hasta ahora”
            asia_start, asia_end = get_session_bounds(date_utc, *SESSIONS["ASIA"])
            london_start, london_end = get_session_bounds(date_utc, *SESSIONS["LONDON"])
            ny_start, ny_end = get_session_bounds(date_utc, *SESSIONS["NY"])

            # --- Mensajes informativos: rango final de sesiones ---
            # 07:00 UTC: Asia final
            if ts_tick >= asia_end and not exists_once(f"{date_utc}_ASIA_FINAL_SENT"):
                asia_df_final = load_rates_utc(symbol, timeframe, asia_start, asia_end)
                if not asia_df_final.empty:
                    asia_hi = float(asia_df_final["high"].max())
                    asia_lo = float(asia_df_final["low"].min())
                    asia_range = (asia_hi - asia_lo) / PIP_SIZE
                    local_h, utc_h = fmt_h(ts_tick, local_tz)

                    msg = (
                        f"✅ EURUSD {tf_name}\n"
                        f"Rango ASIA finalizado\n"
                        f"🕒 Cuba {local_h} | UTC {utc_h}\n"
                        f"📏 Rango: {asia_range:.1f} pips\n"
                        f"📈 {asia_lo:.5f} — {asia_hi:.5f}\n"
                        f"🔎 Desde ahora se monitorean rupturas del rango de ASIA."
                    )
                    if insert_once(f"{date_utc}_ASIA_FINAL_SENT", ts_tick.isoformat(), msg):
                        send_telegram(token, chat_id, msg)

            # 13:00 UTC: Londres final
            if ts_tick >= london_end and not exists_once(f"{date_utc}_LONDON_FINAL_SENT"):
                london_df_final = load_rates_utc(symbol, timeframe, london_start, london_end)
                if not london_df_final.empty:
                    london_hi = float(london_df_final["high"].max())
                    london_lo = float(london_df_final["low"].min())
                    london_range = (london_hi - london_lo) / PIP_SIZE
                    local_h, utc_h = fmt_h(ts_tick, local_tz)

                    msg = (
                        f"✅ EURUSD {tf_name}\n"
                        f"Rango LONDRES finalizado\n"
                        f"🕒 Cuba {local_h} | UTC {utc_h}\n"
                        f"📏 Rango: {london_range:.1f} pips\n"
                        f"📈 {london_lo:.5f} — {london_hi:.5f}\n"
                        f"🔎 Desde ahora se monitorean rupturas del rango de LONDRES (NY)."
                    )
                    if insert_once(f"{date_utc}_LONDON_FINAL_SENT", ts_tick.isoformat(), msg):
                        send_telegram(token, chat_id, msg)

            # --- 1) Alertas de ruptura (ASIA) + reversión ---
            asia_df = load_rates_utc(symbol, timeframe, asia_start, asia_end)
            if not asia_df.empty and ts_tick >= asia_end and ts_tick < ny_end:
                asia_hi = float(asia_df["high"].max())
                asia_lo = float(asia_df["low"].min())

                local_h, utc_h = fmt_h(ts_tick, local_tz)

                # ruptura high
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

                # ruptura low
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

                # reversión tras ruptura (si ya rompió y vuelve dentro del rango)
                if exists_once(f"{date_utc}_ASIA_BROKE_HIGH") and price <= asia_hi - buffer_px:
                    key = f"{date_utc}_ASIA_RETURN_FROM_HIGH"
                    msg = (
                        f"🔁 EURUSD {tf_name}\n"
                        f"Retorno al rango de ASIA (tras romper el máximo)\n"
                        f"🕒 Cuba {local_h} | UTC {utc_h}\n"
                        f"📌 Precio: {price:.5f}\n"
                        f"📈 Asia: {asia_lo:.5f} — {asia_hi:.5f}"
                    )
                    if insert_once(key, ts_tick.isoformat(), msg):
                        send_telegram(token, chat_id, msg)

                if exists_once(f"{date_utc}_ASIA_BROKE_LOW") and price >= asia_lo + buffer_px:
                    key = f"{date_utc}_ASIA_RETURN_FROM_LOW"
                    msg = (
                        f"🔁 EURUSD {tf_name}\n"
                        f"Retorno al rango de ASIA (tras romper el mínimo)\n"
                        f"🕒 Cuba {local_h} | UTC {utc_h}\n"
                        f"📌 Precio: {price:.5f}\n"
                        f"📈 Asia: {asia_lo:.5f} — {asia_hi:.5f}"
                    )
                    if insert_once(key, ts_tick.isoformat(), msg):
                        send_telegram(token, chat_id, msg)

            # --- 2) NY rango extremo (p90) ---
            if ny_p90 is not None and ts_tick >= ny_start and ts_tick < ny_end:
                ny_df = load_rates_utc(symbol, timeframe, ny_start, ts_tick)
                if len(ny_df) >= 5:
                    ny_hi = float(ny_df["high"].max())
                    ny_lo = float(ny_df["low"].min())
                    ny_range_pips = (ny_hi - ny_lo) / PIP_SIZE

                    if ny_range_pips >= ny_p90:
                        local_h, utc_h = fmt_h(ts_tick, local_tz)
                        key = f"{date_utc}_NY_RANGE_EXTREME_P90"
                        msg = (
                            f"🔥 EURUSD {tf_name}\n"
                            f"NY con rango EXTREMO (≥ p90 histórico)\n"
                            f"🕒 Cuba {local_h} | UTC {utc_h}\n"
                            f"📏 Rango NY hoy: {ny_range_pips:.1f} pips\n"
                            f"📊 p90 (últimos días): {ny_p90:.1f} pips"
                        )
                        if insert_once(key, ts_tick.isoformat(), msg):
                            send_telegram(token, chat_id, msg)

            # --- 3) Volatilidad alta (ATR p90) ---
            if atr_p90 is not None:
                atr_now = atr_current_from_mt5(symbol, timeframe, bars=300, period=14)
                if atr_now is not None and atr_now >= atr_p90:
                    local_h, utc_h = fmt_h(ts_tick, local_tz)
                    key = f"{date_utc}_ATR_HIGH_P90"
                    msg = (
                        f"⚡ EURUSD {tf_name}\n"
                        f"Volatilidad ALTA (ATR ≥ p90)\n"
                        f"🕒 Cuba {local_h} | UTC {utc_h}\n"
                        f"📈 ATR(14) actual: {atr_now:.1f} pips\n"
                        f"📊 ATR p90 histórico: {atr_p90:.1f} pips"
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