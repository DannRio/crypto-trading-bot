from market_data import get_ohlcv


symbol = "ZEC/USD"
timeframe = "1h"

df = get_ohlcv(
    symbol=symbol,
    timeframe=timeframe,
    limit=200
)

print(f"\n{symbol} - {timeframe} candles")
print(df.tail(10))