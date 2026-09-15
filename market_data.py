import ccxt


def create_exchange():
    exchange = ccxt.kraken({
        "enableRateLimit": True
    })

    return exchange


def get_ticker(symbol):
    exchange = create_exchange()
    ticker = exchange.fetch_ticker(symbol)

    return ticker