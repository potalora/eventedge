"""Evolution progress dashboard page."""

import os

import streamlit as st
import pandas as pd

from tradingagents.storage.db import Database


def _is_autoresearch_running() -> bool:
    """Check if an autoresearch process is currently running."""
    pid_path = os.path.join(
        os.environ.get("TRADINGAGENTS_RESULTS", "./results"), "autoresearch.pid"
    )
    if not os.path.exists(pid_path):
        return False
    try:
        with open(pid_path) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
        return True
    except (ValueError, OSError, ProcessLookupError):
        return False


def render(db: Database):
    st.title("Evolution Progress")

    running = _is_autoresearch_running()
    if running:
        st.success("Autoresearch is running — refresh page to see updates")
    else:
        st.caption("Autoresearch is idle")

    # Get reflections for generation tracking
    reflections = db.get_reflections()

    if not reflections:
        st.info("No evolution data yet. Run strategies to start evolving strategies.")
        return

    # Generation progress
    st.subheader("Generations Completed")
    st.metric("Total Generations", len(reflections))

    # Fitness per generation chart
    st.subheader("Best Fitness per Generation")
    gen_data = []
    for ref in reflections:
        gen = ref["generation"]
        gen_strategies = db.get_strategies_by_generation(gen)
        if gen_strategies:
            best_fitness = max(s.get("fitness_score", 0) for s in gen_strategies)
        else:
            best_fitness = 0
        gen_data.append({"Generation": gen, "Best Fitness": best_fitness})

    if gen_data:
        df = pd.DataFrame(gen_data)
        st.line_chart(df.set_index("Generation"))

    # Reflections accordion
    st.subheader("Generation Reflections")
    for ref in reversed(reflections):
        with st.expander(f"Generation {ref['generation']}"):
            works = ref.get("patterns_that_work", [])
            fails = ref.get("patterns_that_fail", [])
            guidance = ref.get("next_generation_guidance", [])

            if works:
                st.markdown("**Patterns that work:**")
                for p in works:
                    st.markdown(f"- {p}")
            if fails:
                st.markdown("**Patterns that fail:**")
                for p in fails:
                    st.markdown(f"- {p}")
            if guidance:
                st.markdown("**Next generation guidance:**")
                for g in guidance:
                    st.markdown(f"- {g}")
            if ref.get("regime_notes"):
                st.markdown(f"**Regime notes:** {ref['regime_notes']}")

    # Analyst weight bar chart
    st.subheader("Analyst Weights")
    weights = db.get_analyst_weights()
    if weights:
        df_weights = pd.DataFrame(
            [{"Analyst": k, "Weight": v} for k, v in weights.items()]
        )
        st.bar_chart(df_weights.set_index("Analyst"))
    else:
        st.info("No analyst weights yet. Weights update after backtesting.")
