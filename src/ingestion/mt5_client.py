import MetaTrader5 as mt5

def connect():
    ok = mt5.initialize()
    print("initialize() ->", ok)
    if ok:
        print("terminal_info:", mt5.terminal_info())
        print("account_info:", mt5.account_info())
        return True

    info = mt5.last_error()
    raise RuntimeError(f"MT5 initialize() failed: {info}")

def shutdown():
    mt5.shutdown()
    print("shutdown() OK")

if __name__ == "__main__":
    connect()
    shutdown()