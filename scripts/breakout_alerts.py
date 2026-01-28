import argparse
import sqlite3
import pandas as pd
from datetime import datetime, timezone

DB_PATH="data/eurusd.sqlite"
SYMBOL="EURUSD"
TIMEFRAME="M5"

PIP_SIZE=0.0001

def load_range(db_path:str,date_utc:str,start_hms:str,end_hms:str)->pd.DataFrame:
    start = f"{date_utc}T{start_hms}+00:00"
    end = f"{date_utc}T{end_hms}+00:00"

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
        df=pd.read_sql_query(q, con, params=(SYMBOL,TIMEFRAME, start, end))

    if df.empty:
        return df
    
    df["time_utc"]=pd.to_datetime(df["time_utc"],utc=True)
    return df

def find_breackouts(df_after: pd.DataFrame, hi: float, lo: float, label: str):
    alerts=[]
    broken_high=False
    broken_low=False

    for r in df_after.itertuples(index=False):
        t=r.time_utc
        c=float(r.close)
        
        if (not broken_high) and c>hi:
            broken_high=True
            alerts.append((t,f"{label}: BROKE HIGH",c, hi,lo))
        
        if (not broken_low) and c<lo:
            broken_low=True
            alerts.append((t,f"{label}: BROKE_LOW", c, hi, lo))

        if broken_high and broken_low:
            break

    return alerts

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--date", help="UTC date YYYY-MM-DD (default: today UTC)")
    args=parser.parse_args()
    
    date_utc=args.date or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    #Asia session range (00:00-07:00)
    asia=load_range(DB_PATH,date_utc, "00:00:00","07:00:00")
    if asia.empty:
        print("No Asia Data")
        return 
    
    asia_hi=float(asia["high"].max())
    asia_lo=float(asia["low"].min())

    # After Asia: London+NY (07:00-22:00)
    after_asia=load_range(DB_PATH,date_utc,"07:00:00","22:00:00")
    asia_alerts=find_breackouts(after_asia, asia_hi, asia_lo, "ASIA RANGE")

    # London session range (07:00-13:00) 
    london=load_range(DB_PATH, date_utc, "07:00:00","13:00:00")
    if london.empty:
        print("No London data")
        return
    london_hi=float(london["high"].max())
    london_lo=float(london["low"].min())

    # After London: NY (13:00-22:00)
    after_london=load_range(DB_PATH,date_utc,"13:00:00","22:00:00")
    london_alerts= find_breackouts(after_london, london_hi,london_lo,"LONDON RANGE")

    print(f"\nBreakout alerts for {SYMBOL} {TIMEFRAME} on {date_utc} (UTC)")
    print("-" * 65)
    print(f"Asia   high={asia_hi:.5f} low={asia_lo:.5f}  (~{(asia_hi-asia_lo)/PIP_SIZE:.1f} pips)")
    print(f"London high={london_hi:.5f} low={london_lo:.5f} (~{(london_hi-london_lo)/PIP_SIZE:.1f} pips)")
    print()

    if not asia_alerts and not london_alerts:
        print("No breackouts detected")
        return
    
    for t, msg, c, hi, lo in (asia_alerts + london_alerts):
        print(f"{t.strftime('%Y-%m-%d %H:%M')}  {msg}  close={c:.5f}  (hi={hi:.5f} lo={lo:.5f})")

if __name__=="__main__":
    main()