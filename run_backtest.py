"""Run a baseline backtest using the same confirmed setups as the scanner."""
from trading_bot.analysis import add_indicators
from trading_bot.backtesting import run_backtest
from trading_bot.data import get_ohlcv, validate_ohlcv

EXCHANGE = "kraken"
MARKET_TYPE = "spot"
SYMBOL = "ZEC/USD"
TIMEFRAME = "1h"
CANDLE_LIMIT = 720
INITIAL_EQUITY = 10_000
RISK_PER_TRADE = 0.01
MIN_CONFIRMED_SCORE = 70

# Illustrative assumptions; replace with instrument/venue-specific costs.
FEE_RATE = 0.001
SLIPPAGE_BPS = 5.0


def main():
    df = get_ohlcv(
        symbol=SYMBOL, timeframe=TIMEFRAME, limit=CANDLE_LIMIT,
        exchange_id=EXCHANGE, market_type=MARKET_TYPE,
    )
    if df.empty:
        print("No market data received.")
        return

    closed_candles = df.iloc[:-1].copy()
    quality = validate_ohlcv(closed_candles, timeframe=TIMEFRAME, min_rows=220)
    if not quality.valid:
        print("Backtest rejected invalid market data:", "; ".join(quality.issues))
        return
    if quality.warnings:
        print("Data quality warnings:", "; ".join(quality.warnings))

    result = run_backtest(
        add_indicators(closed_candles),
        initial_equity=INITIAL_EQUITY,
        risk_percent=RISK_PER_TRADE,
        min_confirmed_score=MIN_CONFIRMED_SCORE,
        fee_rate=FEE_RATE,
        slippage_bps=SLIPPAGE_BPS,
        allow_short=MARKET_TYPE != "spot",
    )

    print("\n=== SCANNER-ALIGNED BACKTEST ===")
    print(f"Exchange/market: {EXCHANGE} / {MARKET_TYPE}")
    print(f"Symbol/timeframe: {SYMBOL} / {TIMEFRAME}")
    print(f"Starting equity:  {result.initial_equity:,.2f}")
    print(f"Final equity:     {result.final_equity:,.2f}")
    print(f"Return:           {result.total_return_pct:.2f}%")
    print(f"Max drawdown:     {result.max_drawdown_pct:.2f}%")
    print(f"Signals:          {result.signals_seen}")
    print(f"Skipped entries:  {result.entries_skipped}")
    print(f"Completed trades: {result.total_trades}")
    print(f"Win rate:         {result.win_rate_pct:.2f}%")
    print(f"Profit factor:    {result.profit_factor:.2f}")
    print(f"Average P&L:      {result.average_pnl:.2f}")

    if not result.trades.empty:
        print("\n=== LAST 10 TRADES ===")
        print(result.trades.tail(10).to_string(index=False))

    print("\nResearch only: no funding, liquidation, partial fills or contract multipliers.")


if __name__ == "__main__":
    main()
