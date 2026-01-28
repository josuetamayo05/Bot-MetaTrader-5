import sqlite3
import MetaTrader5 as mt5
import pandas as pd
from datetime import datetime, timedelta, timezone
from pathlib import Path

SYMBOL = "EURUSD"
TIMEFRAME = mt5.TIMEFRAME_M5
DAYS = 90
DB_PATH = "data/eurusd.sqlite"

CREATE_SQL = """
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

INSERT_SQL = """
INSERT OR REPLACE INTO candles
(symbol, timeframe, time_utc, open, high, low, close, tick_volume, spread, real_volume)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
"""

def main():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)

    if not mt5.initialize():
        raise RuntimeError(f"MT5 init failed: {mt5.last_error()}")

    try:
        if not mt5.symbol_select(SYMBOL, True):
            raise RuntimeError(f"symbol_select failed: {mt5.last_error()}")

        utc_to = datetime.now(timezone.utc)
        utc_from = utc_to - timedelta(days=DAYS)

        rates = mt5.copy_rates_range(SYMBOL, TIMEFRAME, utc_from, utc_to)
        if rates is None or len(rates) == 0:
            raise RuntimeError(f"No rates returned: {mt5.last_error()}")

        df = pd.DataFrame(rates)
        df["time_utc"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df = df.drop(columns=["time"])
        df["symbol"] = SYMBOL
        df["timeframe"] = "M5"

        rows = [
            (
                r.symbol, r.timeframe, r.time_utc.isoformat(),
                float(r.open), float(r.high), float(r.low), float(r.close),
                int(r.tick_volume) if pd.notna(r.tick_volume) else None,
                int(r.spread) if pd.notna(r.spread) else None,
                int(r.real_volume) if pd.notna(r.real_volume) else None,
            )
            for r in df.itertuples(index=False)
        ]

        with sqlite3.connect(DB_PATH) as con:
            con.execute(CREATE_SQL)
            con.executemany(INSERT_SQL, rows)
            con.commit()

        print(f"Saved {len(df)} candles into {DB_PATH}")
        print(df.tail(3)[["time_utc","open","high","low","close"]])

    finally:
        mt5.shutdown()

if __name__ == "__main__":
    main()