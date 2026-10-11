"""Bounded candidate dispatch without changing validation or model budgets.

The callback owns existing candidate validation and mutation. Every input gets
an ordered outcome; callers must apply their whole-sample failure policy to
these outcomes. Native transport remains responsible for killing/reaping calls.
"""
from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextvars import copy_context
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from .runtime_deadline import ModelDeadlineExceeded, model_timeout


@dataclass(frozen=True)
class CandidateAnalysisOutcome:
    candidate: Any
    failure: str = ""


def _analyze_one(candidate, analyzer, callback):
    try:
        model_timeout()
        analyzer.last_call_provenance = {}
        analyzer.last_call_failure = ""
        callback(candidate, analyzer)
        model_timeout()
        if analyzer.last_call_failure == "model_deadline_exhausted":
            raise ModelDeadlineExceeded("model_deadline_exhausted")
        if analyzer.last_call_failure in {"model_unavailable", "analysis_unavailable"}:
            return CandidateAnalysisOutcome(candidate, "analysis_unavailable")
        return CandidateAnalysisOutcome(candidate)
    except ModelDeadlineExceeded:
        return CandidateAnalysisOutcome(candidate, "model_deadline_exhausted")
    except Exception:
        # Provider messages and payloads never enter durable diagnostics here.
        return CandidateAnalysisOutcome(candidate, "analysis_unavailable")


def _check_sample_deadline(outcomes):
    try:
        model_timeout()
    except ModelDeadlineExceeded:
        return [CandidateAnalysisOutcome(row.candidate, "model_deadline_exhausted") for row in outcomes]
    return outcomes


def analyze_candidates(candidates: Iterable, *, analyzer: Any,
                       analyze_one: Callable, max_workers: int = 1) -> list[CandidateAnalysisOutcome]:
    """Run a mutating per-candidate callback with the caller's original context.

    No budget is created here. Parallel callbacks use independent analyzer
    diagnostics and a separate Context copy; shared context values such as the
    invocation's validated-response memo retain their original identity.
    At most ``max_workers`` futures exist, including queued work. Worker
    threads cannot kill arbitrary Python callbacks; native calls must continue
    to use the existing deadline-bounded, killable subprocess transport.
    """
    if type(max_workers) is not int or not 1 <= max_workers <= 64:
        raise ValueError("invalid_candidate_workers")
    inputs = list(candidates)
    if max_workers == 1:
        return _check_sample_deadline([_analyze_one(candidate, analyzer, analyze_one) for candidate in inputs])
    if not inputs:
        return []
    try:
        model_timeout()
    except ModelDeadlineExceeded:
        return [CandidateAnalysisOutcome(candidate, "model_deadline_exhausted") for candidate in inputs]

    # Resolve forks in the caller before dispatch. Unsupported injected clients
    # fail explicitly rather than rebuilding credentials/endpoints from env.
    workers = [analyzer.fork_for_parallel() for _ in range(min(max_workers, len(inputs)))]
    outcomes: list[CandidateAnalysisOutcome | None] = [None] * len(inputs)
    next_index = 0
    with ThreadPoolExecutor(max_workers=len(workers), thread_name_prefix="candidate-analysis") as executor:
        pending = {}
        available = workers[:]
        while next_index < len(inputs) or pending:
            while available and next_index < len(inputs):
                index = next_index
                try:
                    model_timeout()
                except ModelDeadlineExceeded:
                    for remaining in range(index, len(inputs)):
                        outcomes[remaining] = CandidateAnalysisOutcome(inputs[remaining], "model_deadline_exhausted")
                    next_index = len(inputs)
                    break
                worker = available.pop()
                context = copy_context()
                future = executor.submit(context.run, _analyze_one, inputs[index], worker, analyze_one)
                pending[future] = (index, worker)
                next_index += 1
            if not pending:
                continue
            try:
                timeout = model_timeout()
            except ModelDeadlineExceeded:
                # Stop dispatch; existing native subprocesses use this same
                # absolute deadline and must be reaped before returning.
                for remaining in range(next_index, len(inputs)):
                    outcomes[remaining] = CandidateAnalysisOutcome(inputs[remaining], "model_deadline_exhausted")
                next_index = len(inputs)
                timeout = None
            completed, _ = wait(pending, timeout=timeout, return_when=FIRST_COMPLETED)
            for future in completed:
                index, worker = pending.pop(future)
                outcomes[index] = future.result()
                available.append(worker)
    # The loop accounts for every index; do not filter away missing outcomes.
    assert all(outcome is not None for outcome in outcomes)
    return _check_sample_deadline(outcomes)
