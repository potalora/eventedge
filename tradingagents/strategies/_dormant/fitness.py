from typing import Dict, List
from tradingagents.strategies.state.models import Strategy, BacktestResults
from tradingagents.storage.db import Database


def compute_fitness(strategy: Strategy, config: dict) -> float:
    """Compute fitness score per spec formula.

    fitness = sharpe * min(profit_factor, 3.0) * (1 - abs(max_drawdown))
    complexity_penalty = 1.0 / (1.0 + penalty_factor * complexity)
    fitness *= complexity_penalty

    Returns 0.0 if backtest_results is None or num_trades < min_trades.
    """
    ar = config.get("autoresearch", {})
    min_trades = ar.get("min_trades_for_scoring", 5)
    penalty_factor = ar.get("complexity_penalty_factor", 0.1)

    br = strategy.backtest_results
    if br is None or br.num_trades < min_trades:
        return 0.0

    base = br.sharpe * min(br.profit_factor, 3.0) * (1 - abs(br.max_drawdown))

    num_filters = len(strategy.screener.custom_filters)
    num_rules = len(strategy.entry_rules)
    complexity = num_filters + num_rules
    complexity_penalty = 1.0 / (1.0 + penalty_factor * complexity)

    return base * complexity_penalty


def rank_strategies(strategies: list[Strategy], config: dict) -> list[Strategy]:
    """Sort strategies by fitness descending. Apply tiebreakers:
    1. Higher win_rate
    2. More trades (statistical significance)
    Strategies with insufficient trades get fitness = 0.
    Returns sorted list with fitness_score set on each.
    """
    for s in strategies:
        s.fitness_score = compute_fitness(s, config)

    def sort_key(s):
        br = s.backtest_results
        win_rate = br.win_rate if br else 0.0
        num_trades = br.num_trades if br else 0
        return (s.fitness_score, win_rate, num_trades)

    return sorted(strategies, key=sort_key, reverse=True)


def meets_paper_criteria(strategy: Strategy, config: dict) -> bool:
    """Check if strategy qualifies for paper trading:
    - sharpe > fitness_min_sharpe across walk-forward windows
    - num_trades >= fitness_min_trades
    - win_rate > fitness_min_win_rate
    - has backtest_results
    - holdout_sharpe degradation < 30% vs mean walk_forward_scores
    """
    ar = config.get("autoresearch", {})
    br = strategy.backtest_results
    if br is None:
        return False
    if br.sharpe <= ar.get("fitness_min_sharpe", 1.0):
        return False
    if br.num_trades < ar.get("fitness_min_trades", 10):
        return False
    if br.win_rate <= ar.get("fitness_min_win_rate", 0.50):
        return False
    # Holdout check
    if br.holdout_sharpe is not None and br.walk_forward_scores:
        mean_wf = sum(br.walk_forward_scores) / len(br.walk_forward_scores)
        if mean_wf > 0 and br.holdout_sharpe < mean_wf * 0.7:
            return False
    return True


def meets_graduation_criteria(strategy: Strategy, paper_trades: list[dict], config: dict) -> bool:
    """Check if paper trading results warrant READY status:
    - len(completed_trades) >= paper_min_trades (trades with both entry and exit)
    - paper_win_rate within paper_max_divergence_pct of backtest win_rate
    - paper_sharpe > 0.5
    - no single trade lost > 2x stated max_risk_pct
    """
    ar = config.get("autoresearch", {})
    min_trades = ar.get("paper_min_trades", 5)
    max_divergence = ar.get("paper_max_divergence_pct", 15)

    completed = [t for t in paper_trades if t.get("exit_date") is not None]
    if len(completed) < min_trades:
        return False

    # Paper win rate
    winners = [t for t in completed if (t.get("pnl", 0) or 0) > 0]
    paper_win_rate = len(winners) / len(completed) if completed else 0.0

    # Divergence from backtest
    br = strategy.backtest_results
    if br:
        divergence = abs(paper_win_rate - br.win_rate) * 100
        if divergence > max_divergence:
            return False

    # Paper Sharpe > 0.5 (simplified: use mean return / std return)
    returns = [t.get("pnl_pct", 0) or 0 for t in completed]
    if len(returns) >= 2:
        import statistics
        mean_r = statistics.mean(returns)
        std_r = statistics.stdev(returns)
        paper_sharpe = (mean_r / std_r) if std_r > 0 else 0.0
        if paper_sharpe < 0.5:
            return False

    # No single trade lost > 2x max_risk_pct
    max_risk = strategy.max_risk_pct
    for t in completed:
        pnl_pct = t.get("pnl_pct", 0) or 0
        if pnl_pct < -(2 * max_risk):
            return False

    return True


def check_failure_criteria(strategy: Strategy, paper_trades: list[dict]) -> bool:
    """Return True if strategy should be marked FAILED:
    - win_rate > 20 points below backtest
    - 3 consecutive losses
    - sharpe < 0 after 5+ trades
    """
    completed = [t for t in paper_trades if t.get("exit_date") is not None]
    if not completed:
        return False

    # Win rate check
    winners = [t for t in completed if (t.get("pnl", 0) or 0) > 0]
    paper_win_rate = len(winners) / len(completed)
    br = strategy.backtest_results
    if br and (br.win_rate - paper_win_rate) > 0.20:
        return True

    # 3 consecutive losses
    if len(completed) >= 3:
        for i in range(len(completed) - 2):
            if all((completed[i + j].get("pnl", 0) or 0) < 0 for j in range(3)):
                return True

    # Sharpe < 0 after 5+ trades
    if len(completed) >= 5:
        import statistics
        returns = [t.get("pnl_pct", 0) or 0 for t in completed]
        mean_r = statistics.mean(returns)
        std_r = statistics.stdev(returns)
        paper_sharpe = (mean_r / std_r) if std_r > 0 else 0.0
        if paper_sharpe < 0:
            return True

    return False


def update_analyst_weights(db: Database, trade_results: list[dict], config: dict) -> Dict[str, float]:
    """Darwinian analyst weight update.

    trade_results: list of dicts with keys: pnl (float), analyst_scores (dict like {"market": 1, "news": -1, ...})

    For each analyst:
    - Sum their scores across all trades
    - Top quartile: weight * 1.05
    - Bottom quartile: weight * 0.95
    - Clamp to [analyst_weight_min, analyst_weight_max]
    Persist via db.upsert_analyst_weight()
    Returns updated weights dict.
    """
    ar = config.get("autoresearch", {})
    weight_min = ar.get("analyst_weight_min", 0.3)
    weight_max = ar.get("analyst_weight_max", 2.5)

    # Get current weights
    current_weights = db.get_analyst_weights()
    analysts = ["market", "news", "sentiment", "fundamentals", "options"]
    for a in analysts:
        if a not in current_weights:
            current_weights[a] = 1.0

    # Score each analyst
    analyst_total_scores = {a: 0 for a in analysts}
    for trade in trade_results:
        scores = trade.get("analyst_scores", {})
        for a in analysts:
            analyst_total_scores[a] += scores.get(a, 0)

    # Rank analysts
    sorted_analysts = sorted(analysts, key=lambda a: analyst_total_scores[a])
    n = len(sorted_analysts)
    q1_cutoff = n // 4  # bottom quartile index
    q3_cutoff = n - n // 4  # top quartile index

    for i, a in enumerate(sorted_analysts):
        w = current_weights[a]
        if i < q1_cutoff:
            w *= 0.95  # bottom quartile
        elif i >= q3_cutoff:
            w *= 1.05  # top quartile
        w = max(weight_min, min(weight_max, w))
        current_weights[a] = round(w, 4)
        db.upsert_analyst_weight(a, current_weights[a])

    return current_weights
