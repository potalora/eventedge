import json

import pandas as pd
import streamlit as st

from tradingagents.storage.db import Database
from tradingagents.dashboard.components.charts import make_equity_curve_chart


def render(db: Database):
    st.title("Backtest Results")

    rows = db.conn.execute(
        "SELECT * FROM backtest_runs ORDER BY created_at DESC LIMIT 10"
    ).fetchall()

    if not rows:
        st.info("No backtest runs yet. Run `tradingagents backtest` to generate results.")
        return

    runs = [dict(r) for r in rows]
    labels = [
        f"#{r['id']} — {json.loads(r['tickers'])} ({r['start_date']} to {r['end_date']})"
        for r in runs
    ]
    selected_idx = st.selectbox("Select Run", range(len(labels)), format_func=lambda i: labels[i])
    run = runs[selected_idx]

    metrics = json.loads(run["metrics"])
    st.subheader("Performance Metrics")
    cols = st.columns(4)
    metric_items = list(metrics.items())
    for i, (k, v) in enumerate(metric_items[:8]):
        with cols[i % 4]:
            display_val = f"{v:.4f}" if isinstance(v, float) else str(v)
            st.metric(k.replace("_", " ").title(), display_val)

    snapshots = db.get_equity_curve(run["id"])
    if snapshots:
        df = pd.DataFrame(snapshots)
        st.plotly_chart(make_equity_curve_chart(df), use_container_width=True)
