import pandas as pd


def calculate_trade_metrics(
    trades: pd.DataFrame,
) -> tuple[int, float, float, float]:
    total_trades = len(trades)

    if total_trades == 0:
        return 0, 0.0, 0.0, 0.0

    wins = trades[trades["pnl"] > 0]
    losses = trades[trades["pnl"] < 0]

    win_rate = (
        len(wins) / total_trades
    ) * 100

    gross_profit = wins["pnl"].sum()
    gross_loss = abs(losses["pnl"].sum())

    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    elif gross_profit > 0:
        profit_factor = float("inf")
    else:
        profit_factor = 0.0

    average_pnl = trades["pnl"].mean()

    return (
        total_trades,
        win_rate,
        profit_factor,
        average_pnl,
    )


def calculate_return_pct(
    initial_equity: float,
    final_equity: float,
) -> float:
    return (
        (final_equity / initial_equity) - 1
    ) * 100