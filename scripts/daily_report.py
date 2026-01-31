import sqlite3
import yaml
import requests
import pandas as pd
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

from pathlib import Path
import sys
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from src.reports.pdf_pro import build_pro_pdf

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

DB_PATH = "data/eurusd.sqlite"
SYMBOL = "EURUSD"
TIMEFRAME = "M5"
PIP_SIZE = 0.0001

SESSIONS = [
    ("ASIA", "00:00:00", "07:00:00"),
    ("LONDON", "07:00:00", "13:00:00"),
    ("NY", "13:00:00", "22:00:00"),
]

def send_telegram_text(token: str, chat_id: int, text: str):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    r = requests.post(url, json={"chat_id": chat_id, "text": text})
    r.raise_for_status()

def send_telegram_document(token: str, chat_id: int, file_path: str, caption: str = ""):
    url = f"https://api.telegram.org/bot{token}/sendDocument"
    with open(file_path, "rb") as f:
        r = requests.post(url, data={"chat_id": chat_id, "caption": caption}, files={"document": f})
    r.raise_for_status()

def ensure_session_summary_table():
    sql = """
    CREATE TABLE IF NOT EXISTS session_summary (
      date_utc TEXT NOT NULL,
      session  TEXT NOT NULL,
      start_utc TEXT NOT NULL,
      end_utc   TEXT NOT NULL,
      open REAL,
      close REAL,
      high REAL,
      low REAL,
      range_pips REAL,
      bars INTEGER,
      PRIMARY KEY (date_utc, session)
    );
    """
    with sqlite3.connect(DB_PATH) as con:
        con.execute(sql)
        con.commit()

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

def load_day_candles(date_utc: str) -> pd.DataFrame:
    start = f"{date_utc}T00:00:00+00:00"
    end   = f"{date_utc}T23:59:59+00:00"
    q = """
    SELECT time_utc, open, high, low, close
    FROM candles
    WHERE symbol = ?
      AND timeframe = ?
      AND time_utc >= ?
      AND time_utc <= ?
    ORDER BY time_utc ASC;
    """
    with sqlite3.connect(DB_PATH) as con:
        df = pd.read_sql_query(q, con, params=(SYMBOL, TIMEFRAME, start, end))
    if df.empty:
        return df
    df["time_utc"] = pd.to_datetime(df["time_utc"], utc=True)
    return df

def slice_session(df: pd.DataFrame, date_utc: str, start_hms: str, end_hms: str) -> pd.DataFrame:
    start = pd.Timestamp(f"{date_utc} {start_hms}", tz="UTC")
    end   = pd.Timestamp(f"{date_utc} {end_hms}", tz="UTC")
    return df[(df["time_utc"] >= start) & (df["time_utc"] < end)]

def upsert_session_row(row):
    sql = """
    INSERT OR REPLACE INTO session_summary
    (date_utc, session, start_utc, end_utc, open, close, high, low, range_pips, bars)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
    """
    with sqlite3.connect(DB_PATH) as con:
        con.execute(sql, row)
        con.commit()

def build_session_summary_for_date(date_utc: str):
    ensure_session_summary_table()
    df = load_day_candles(date_utc)
    if df.empty:
        return False

    for name, s, e in SESSIONS:
        sdf = slice_session(df, date_utc, s, e)
        start_iso = f"{date_utc}T{s}+00:00"
        end_iso   = f"{date_utc}T{e}+00:00"

        if sdf.empty:
            row = (date_utc, name, start_iso, end_iso, None, None, None, None, None, 0)
        else:
            o = float(sdf.iloc[0]["open"])
            c = float(sdf.iloc[-1]["close"])
            hi = float(sdf["high"].max())
            lo = float(sdf["low"].min())
            rp = (hi - lo) / PIP_SIZE
            row = (date_utc, name, start_iso, end_iso, o, c, hi, lo, float(rp), int(len(sdf)))

        upsert_session_row(row)

    return True

def get_session_rows(date_utc: str):
    q = """
    SELECT session, open, close, high, low, range_pips, bars
    FROM session_summary
    WHERE date_utc = ?
    ORDER BY session;
    """
    with sqlite3.connect(DB_PATH) as con:
        return con.execute(q, (date_utc,)).fetchall()

def get_alerts_for_date(date_utc: str):
    # Solo las que comienzan con "YYYY-MM-DD_" (evita BOT_ACTIVE)
    q = """
    SELECT first_ts_utc, alert_key, message
    FROM alerts_once
    WHERE alert_key LIKE ?
    ORDER BY first_ts_utc ASC;
    """
    like = f"{date_utc}_%"
    with sqlite3.connect(DB_PATH) as con:
        return con.execute(q, (like,)).fetchall()

def ny_p90(lookback_days: int = 60) -> float | None:
    q = """
    SELECT range_pips
    FROM session_summary
    WHERE session='NY' AND range_pips IS NOT NULL AND bars > 0
    ORDER BY date_utc DESC
    LIMIT ?;
    """
    with sqlite3.connect(DB_PATH) as con:
        rows = con.execute(q, (lookback_days,)).fetchall()
    if not rows:
        return None
    s = pd.Series([float(r[0]) for r in rows])
    return float(s.quantile(0.90))

def make_pdf(date_utc: str, rows, alerts, out_path: str):
    c = canvas.Canvas(out_path, pagesize=A4)
    w, h = A4
    y = h - 60

    c.setFont("Helvetica-Bold", 16)
    c.drawString(50, y, f"EURUSD Reporte Diario (UTC) — {date_utc}")
    y -= 30

    c.setFont("Helvetica", 11)
    c.drawString(50, y, "Sesiones (rangos en pips):")
    y -= 18

    c.setFont("Helvetica", 10)
    for session, o, cl, hi, lo, rp, bars in rows:
        if rp is None:
            line = f"{session}: (sin datos)"
        else:
            line = f"{session}: rango={rp:.1f} pips | O={o:.5f} C={cl:.5f} | H={hi:.5f} L={lo:.5f} | velas={bars}"
        c.drawString(50, y, line[:110])
        y -= 14
        if y < 120:
            c.showPage()
            y = h - 60
            c.setFont("Helvetica", 10)

    y -= 10
    c.setFont("Helvetica-Bold", 11)
    c.drawString(50, y, "Alertas del día (registradas):")
    y -= 18

    c.setFont("Helvetica", 9)
    if not alerts:
        c.drawString(50, y, "No hubo alertas registradas.")
        y -= 14
    else:
        for ts, key, msg in alerts:
            c.drawString(50, y, f"- {ts} | {key}")
            y -= 12
            if y < 80:
                c.showPage()
                y = h - 60
                c.setFont("Helvetica", 9)

    c.showPage()
    c.save()

def choose_report_date_utc() -> str:
    # Reporte del ÚLTIMO día UTC completado:
    # si ahora UTC < 22:05 => reporta AYER, si no => HOY
    now = datetime.now(timezone.utc)
    cutoff = now.replace(hour=22, minute=5, second=0, microsecond=0)
    if now < cutoff:
        d = (now.date() - timedelta(days=1))
    else:
        d = now.date()
    return d.strftime("%Y-%m-%d")

def main():
    cfg = yaml.safe_load(open("src/config/settings.yaml", "r", encoding="utf-8"))
    token = cfg["telegram"]["token"]
    chat_id = int(cfg["telegram"]["chat_id"])
    local_tz = ZoneInfo(cfg.get("timezone", {}).get("local", "America/Havana"))

    ensure_alerts_once_table()

    date_utc = choose_report_date_utc()
    report_key = f"REPORT_SENT_{date_utc}"

    # evita enviar 2 veces si el scheduler corre más de una vez
    if not insert_once(report_key, datetime.now(timezone.utc).isoformat(), f"Reporte enviado {date_utc}"):
        return

    ok = build_session_summary_for_date(date_utc)
    if not ok:
        send_telegram_text(token, chat_id, f"⚠️ No hay datos suficientes en DB para generar reporte {date_utc}.")
        return

    rows = get_session_rows(date_utc)
    alerts = get_alerts_for_date(date_utc)

    # resumen corto
    ny_p90_val = ny_p90(60)
    now_local = datetime.now(timezone.utc).astimezone(local_tz).strftime("%H:%M")
    lines = [f"📄 Reporte diario EURUSD (UTC) — {date_utc}",
             f"Hora Cuba: {now_local}"]

    # extrae rangos
    ranges = {r[0]: r[5] for r in rows if r[5] is not None}
    if "ASIA" in ranges: lines.append(f"ASIA: {ranges['ASIA']:.1f} pips")
    if "LONDON" in ranges: lines.append(f"LONDRES: {ranges['LONDON']:.1f} pips")
    if "NY" in ranges:
        lines.append(f"NY: {ranges['NY']:.1f} pips")
        if ny_p90_val is not None:
            lines.append(f"NY p90 (hist): {ny_p90_val:.1f} pips")

    lines.append(f"Alertas registradas: {len(alerts)}")
    send_telegram_text(token, chat_id, "\n".join(lines))

    # PDF
    Path("reports").mkdir(exist_ok=True)
    pdf_path = f"reports/eurusd_report_{date_utc}.pdf"
    pdf_path = f"reports/eurusd_report_{date_utc}.pdf"
    build_pro_pdf(
        date_utc=date_utc,
        session_rows=rows,     
        alerts_rows=alerts,   
        out_path=pdf_path,
        ny_p90_val=ny_p90_val, 
        brand="JA CubanCode"
    )

    send_telegram_document(token, chat_id, pdf_path, caption=f"Reporte EURUSD {date_utc} (UTC)")
    print("Reporte enviado:", pdf_path)

if __name__ == "__main__":
    main()