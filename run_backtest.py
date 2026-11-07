from trading_bot.backtesting import run_decision_backtest
from trading_bot.data import get_ohlcv


EXCHANGE_ID = "kraken"
MARKET_TYPE = "spot"
SYMBOL = "ZEC/USD"
TIMEFRAME = "1h"
CANDLE_LIMIT = 720

INITIAL_EQUITY = 10_000
RISK_PER_TRADE = 0.01
MIN_CONFIRMED_SCORE = 70

FEE_RATE = 0.001
SLIPPAGE_BPS = 5.0


def main():
    df = get_ohlcv(
        symbol=SYMBOL,
        timeframe=TIMEFRAME,
        limit=CANDLE_LIMIT,
        exchange_id=EXCHANGE_ID,
        market_type=MARKET_TYPE,
    )

    if df.empty:
        print("No market data received.")
        return

    closed_candles = df.iloc[:-1].copy()

    result = run_decision_backtest(
        data=closed_candles,
        initial_equity=INITIAL_EQUITY,
        risk_percent=RISK_PER_TRADE,
        min_confirmed_score=MIN_CONFIRMED_SCORE,
        fee_rate=FEE_RATE,
        slippage_bps=SLIPPAGE_BPS,
        allow_short=MARKET_TYPE != "spot",
    )

    print("\n=== SCANNER-ALIGNED BACKTEST ===")
    print(f"Exchange:          {EXCHANGE_ID}")
    print(f"Market type:       {MARKET_TYPE}")
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
            "score",
            "regime",
            "entry_time",
            "exit_time",
            "entry_price",
            "exit_price",
            "pnl",
            "r_multiple",
            "exit_reason",
        ]
        print(
            result.trades[
                display_columns
            ].tail(10).to_string(index=False)
        )


if __name__ == "__main__":
    main()
