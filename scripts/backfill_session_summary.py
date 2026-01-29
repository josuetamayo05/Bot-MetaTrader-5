import sqlite3
import pandas as pd
from datetime import datetime, timedelta, timezone

DB_PATH = "data/eurusd.sqlite"
SYMBOL = "EURUSD"
TIMEFRAME = "M5"
PIP_SIZE = 0.0001

SESSIONS = [
    ("ASIA",   "00:00:00", "07:00:00"),
    ("LONDON", "07:00:00", "13:00:00"),
    ("NY",     "13:00:00", "22:00:00"),
]

def ensure_table():
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

def load_day(date_utc: str) -> pd.DataFrame:
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

def upsert_row(row):
    sql = """
    INSERT OR REPLACE INTO session_summary
    (date_utc, session, start_utc, end_utc, open, close, high, low, range_pips, bars)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
    """
    with sqlite3.connect(DB_PATH) as con:
        con.execute(sql, row)
        con.commit()

def main(days: int = 60):
    ensure_table()
    today = datetime.now(timezone.utc).date()

    saved = 0
    for i in range(days):
        d = today - timedelta(days=i+1)  # atrás (no incluye hoy)
        date_utc = d.strftime("%Y-%m-%d")

        df = load_day(date_utc)
        if df.empty:
            continue

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

            upsert_row(row)
            saved += 1

    print(f"Backfill listo. Filas guardadas/actualizadas: {saved}")

if __name__ == "__main__":
    main(60)