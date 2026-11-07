from trading_bot.analysis import add_indicators
from trading_bot.backtesting import run_backtest
from trading_bot.data import get_ohlcv


SYMBOL = "ZEC/USD"
TIMEFRAME = "1h"
CANDLE_LIMIT = 500

INITIAL_EQUITY = 10_000
RISK_PER_TRADE = 0.01

# Keep configurable.
# We'll insert realistic exchange costs later.
FEE_RATE = 0.0
SLIPPAGE_BPS = 0.0


def main():

    df = get_ohlcv(
        symbol=SYMBOL,
        timeframe=TIMEFRAME,
        limit=CANDLE_LIMIT,
    )

    if df.empty:
        print("No market data received.")
        return

    # Remove currently forming candle.
    closed_candles = df.iloc[:-1].copy()

    data = add_indicators(closed_candles)

    result = run_backtest(
        data=data,
        initial_equity=INITIAL_EQUITY,
        risk_percent=RISK_PER_TRADE,
        atr_stop_multiplier=2.0,
        reward_to_risk=2.0,
        fee_rate=FEE_RATE,
        slippage_bps=SLIPPAGE_BPS,
    )

    print("\n=== BACKTEST RESULTS ===")
    print(f"Symbol:            {SYMBOL}")
    print(f"Timeframe:         {TIMEFRAME}")
    print(f"Starting equity:   ${result.initial_equity:,.2f}")
    print(f"Final equity:      ${result.final_equity:,.2f}")
    print(f"Return:            {result.total_return_pct:.2f}%")
    print(f"Max drawdown:      {result.max_drawdown_pct:.2f}%")
    print(f"Trades:            {result.total_trades}")
    print(f"Win rate:          {result.win_rate_pct:.2f}%")
    print(f"Profit factor:     {result.profit_factor:.2f}")
    print(f"Average P&L:       ${result.average_pnl:.2f}")

    if not result.trades.empty:

        print("\n=== LAST 10 TRADES ===")

        display_columns = [
            "signal",
            "entry_time",
            "exit_time",
            "entry_price",
            "exit_price",
            "position_size",
            "pnl",
            "exit_reason",
        ]

        print(
            result.trades[
                display_columns
            ].tail(10).to_string(index=False)
        )


if __name__ == "__main__":
    main()