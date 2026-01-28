import MetaTrader5 as mt5
from datetime import datetime, timezone

SYMBOL = "EURUSD"

def main():
    if not mt5.initialize():
        print("init false:", mt5.last_error())
        return

    if not mt5.symbol_select(SYMBOL, True):
        print("symbol_select failed:", mt5.last_error())
        mt5.shutdown()
        return

    rates = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_M5, 0, 20)
    print("rates len:", 0 if rates is None else len(rates))
    if rates is not None and len(rates) > 0:
        print("last bar time:", datetime.fromtimestamp(rates[-1]["time"], tz=timezone.utc))
        print("last bar OHLC:", rates[-1]["open"], rates[-1]["high"], rates[-1]["low"], rates[-1]["close"])

    mt5.shutdown()

if __name__ == "__main__":
    main()