from market_data import get_ticker


symbol = "ZEC/USD"

ticker = get_ticker(symbol)

print(f"Symbol: {symbol}")
print(f"Last price: ${ticker['last']}")
print(f"Bid: ${ticker['bid']}")
print(f"Ask: ${ticker['ask']}")
print(f"24h High: ${ticker['high']}")
print(f"24h Low: ${ticker['low']}")