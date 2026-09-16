import pandas as pd

from market_data import get_ohlcv
from indicators import add_indicators
from strategy import generate_signal


SYMBOL = "ZEC/USD"
TIMEFRAME = "1h"
CANDLE_LIMIT = 500
DISPLAY_ROWS = 10


def main():
    df = get_ohlcv(
        symbol=SYMBOL,
        timeframe=TIMEFRAME,
        limit=CANDLE_LIMIT
    )

    if df.empty:
        print("No market data received.")
        return

    # Never make decisions using the currently forming candle.
    closed_candles = df.iloc[:-1].copy()

    # Calculate indicators.
    data = add_indicators(closed_candles)

    # Generate strategy signal.
    result = generate_signal(data)

    columns = [
        "timestamp",
        "close",
        "ema_50",
        "ema_200",
        "rsi_14",
        "atr_14",
    ]

    display = data[columns].tail(DISPLAY_ROWS).copy()

    display["timestamp"] = display["timestamp"].dt.strftime(
        "%Y-%m-%d %H:%M"
    )

    numeric_columns = [
        "close",
        "ema_50",
        "ema_200",
        "rsi_14",
        "atr_14",
    ]

    display[numeric_columns] = display[numeric_columns].round(2)

    print(f"\n{SYMBOL} - {TIMEFRAME}")
    print(f"Closed candles available: {len(data)}\n")

    print(display.to_string(index=False))

    print("\n--- Strategy ---")
    print(f"Signal: {result.signal.value}")
    print(f"Reason: {result.reason}")


if __name__ == "__main__":
    main()