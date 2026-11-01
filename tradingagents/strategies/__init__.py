"""Public strategy API, loaded when used by the caller.

Transport subprocesses import this package too. Resolving a transport module
must not initialize unrelated portfolio books, cohort engines or calendars.
"""
from importlib import import_module


_EXPORTS = {
    'Filter': 'state.models',
    'ScreenerCriteria': 'state.models',
    'BacktestResults': 'state.models',
    'ScreenerResult': 'state.models',
    'Strategy': 'state.models',
    'MultiStrategyEngine': 'orchestration.multi_strategy_engine',
    'CohortOrchestrator': 'orchestration.cohort_orchestrator',
    'CohortConfig': 'orchestration.cohort_orchestrator',
    'build_default_cohorts': 'orchestration.cohort_orchestrator',
    'CohortComparison': 'orchestration.cohort_comparison',
    'SignalJournal': 'learning.signal_journal',
}
__all__ = list(_EXPORTS)


def __getattr__(name):
    if name in _EXPORTS:
        value = getattr(import_module('.'+_EXPORTS[name], __name__), name)
    elif name in {'state', 'orchestration', 'learning'}:
        # Retain the package attributes exposed by the former eager imports.
        value = import_module('.'+name, __name__)
    else:
        raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__) | {'state', 'orchestration', 'learning'})
