from typing import Any, Dict, List
import numpy as np
import pandas as pd


def compute_metrics(equity_curve: pd.DataFrame, trade_log: List[Dict[str, Any]]) -> Dict[str, Any]:
    values = equity_curve["portfolio_value"].values
    initial = values[0]
    final = values[-1]
    total_return = (final / initial) - 1 if initial > 0 else 0.0
    running_max = np.maximum.accumulate(values)
    drawdowns = (values - running_max) / running_max
    max_drawdown = float(drawdowns.min()) if len(drawdowns) > 0 else 0.0
    returns = np.diff(values) / values[:-1] if len(values) > 1 else np.array([])
    if len(returns) > 1 and np.std(returns) > 0:
        sharpe_ratio = float(np.mean(returns) / np.std(returns) * np.sqrt(52))
    else:
        sharpe_ratio = 0.0
    downside = returns[returns < 0]
    if len(downside) > 0 and np.std(downside) > 0:
        sortino_ratio = float(np.mean(returns) / np.std(downside) * np.sqrt(52))
    else:
        sortino_ratio = 0.0
    pnls = [t.get("pnl", 0) for t in trade_log if t.get("pnl") is not None]
    total_trades = len(pnls)
    winners = [p for p in pnls if p > 0]
    losers = [p for p in pnls if p < 0]
    win_rate = len(winners) / total_trades if total_trades > 0 else 0.0
    gross_profit = sum(winners) if winners else 0.0
    gross_loss = abs(sum(losers)) if losers else 0.0
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf") if gross_profit > 0 else 0.0
    return {
        "total_return": round(total_return, 4),
        "annualized_return": round(total_return * (252 / max(len(values), 1)), 4),
        "sharpe_ratio": round(sharpe_ratio, 4),
        "sortino_ratio": round(sortino_ratio, 4),
        "max_drawdown": round(max_drawdown, 4),
        "total_trades": total_trades,
        "win_rate": round(win_rate, 4),
        "profit_factor": round(profit_factor, 4),
        "avg_pnl": round(np.mean(pnls), 2) if pnls else 0.0,
        "avg_winner": round(np.mean(winners), 2) if winners else 0.0,
        "avg_loser": round(np.mean(losers), 2) if losers else 0.0,
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
    }
