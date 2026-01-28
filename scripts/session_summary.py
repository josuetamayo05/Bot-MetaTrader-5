import argparse
import sqlite3
import pandas as pd
from datetime import datetime,timezone

DB_PATH="data/eurusd.sqlite"
SYMBOL="EURUSD"
TIMEFRAME="M5"

SESSIONS=[
    ("ASIA","00:00:00","07:00:00"),
    ("LONDON", "07:00:00", "13:00:00"),
    ("NY","13:00:00","22:00:00"),
]

PIP_SIZE=0.0001 #EURUSD pip estandar

def load_day(db_path:str,date_utc:str)->pd.DataFrame:
    start=f"{date_utc}T00:00:00+00:00"
    end=f"{date_utc}T23:59:59+00:00"

    q="""
    SELECT time_utc, open, high, low, close, tick_volume
    FROM candles
    WHERE symbol=?
        AND timeframe=?
        AND time_utc>=?
        AND time_utc<=?
    ORDER BY time_utc ASC;
    """

    with sqlite3.connect(db_path) as con:
        df=pd.read_sql_query(q, con, params=(SYMBOL, TIMEFRAME, start, end))

    if df.empty:
        return df

    df["time_utc"]=pd.to_datetime(df["time_utc"],utc=True)
    return df

def session_slice(df:pd.DataFrame,date_utc:str,start_hms:str,end_hms: str)->pd.DataFrame:
    start=pd.Timestamp(f"{date_utc} {start_hms}",tz="UTC")
    end=pd.Timestamp(f"{date_utc} {end_hms}",tz="UTC")
    return df[(df["time_utc"]>=start) & (df["time_utc"]<end)]

def compute_metrics(sdf :pd.DataFrame):
    if sdf.empty:
        return None

    o=float(sdf.iloc[0]["open"])
    c=float(sdf.iloc[-1]["close"])
    hi=float(sdf["high"].max())
    lo=float(sdf["low"].min())
    range_pips=(hi-lo)/PIP_SIZE

    return {
        "open":o,
        "close":c,
        "high":hi,
        "low":lo,
        "range_pips":float(range_pips),
        "bars":int(len(sdf))
    }
    
def ensure_table(db_path:str):
    sql="""
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

    with sqlite3.connect(db_path) as con:
        con.execute(sql)
        con.commit()

def save_rows(db_path:str, rows):
    sql="""
    INSERT OR REPLACE INTO session_summary
    (date_utc, session, start_utc, end_utc, open,close,high, low, range_pips, bars)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    with sqlite3.connect(db_path) as con:
        con.execute(sql, rows)
        con.commit()

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--date", help="UTC date YYYY-MM-DD (default: today UTC)")
    parser.add_argument("--save", action="store_true", help="Save results into SQLite table session_summary")
    args=parser.parse_args()

    date_utc=args.date
    if not date_utc:
        date_utc=datetime.now(timezone.utc).strftime("%Y-%m-%d")
    
    df=load_day(DB_PATH, date_utc)
    if df.empty:
        print(f"No data found for {SYMBOL} {TIMEFRAME} on {date_utc} (UTC)")
        return

    print(f"\nSession summary for {SYMBOL} {TIMEFRAME} on {date_utc} (UTC)")
    print("-" * 60)

    rows_to_save=[]
    for name, start_hms, end_hms in SESSIONS:
        sdf=session_slice(df,date_utc,start_hms,end_hms)
        m=compute_metrics(sdf)

        start_iso=f"{date_utc}T{start_hms}+00:00"
        end_iso=f"{date_utc}T{end_hms}+00:00"

        if m is None:
            print(f"{name:6}  {start_hms}-{end_hms}  (no bars)")
            if args.save:
                rows_to_save.append((date_utc,name,start_iso,end_iso,None,None,None,None,None,0))
            continue
        
        print(
            f"{name:6}  {start_hms}-{end_hms}  "
            f"range={m['range_pips']:.1f} pips  "
            f"O={m['open']:.5f} C={m['close']:.5f}  "
            f"H={m['high']:.5f} L={m['low']:.5f}  "
            f"bars={m['bars']}"
        )

        if args.save:
            rows_to_save.append((
                date_utc, name, start_iso, end_iso,
                m["open"], m["close"], m["high"], m["low"], m["range_pips"], m["bars"]
            ))
        
    if args.save:
        ensure_table(DB_PATH)
        save_rows(DB_PATH, rows_to_save)
        print("\nSaved into SQLite table: session_summary")

if __name__ == "__main__":
    main()