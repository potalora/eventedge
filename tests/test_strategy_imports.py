"""Public strategy imports retain identity without initializing a trading run."""
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def isolated(code):
    result = subprocess.run([sys.executable, '-I', '-c',
        'import sys;sys.path.insert(0,'+repr(str(ROOT))+');'+code],
        capture_output=True,text=True,timeout=30)
    assert result.returncode == 0, result.stdout+result.stderr


def test_transport_submodule_import_does_not_initialize_trading_calendar():
    isolated('''
import tradingagents.strategies.transport_worker
assert 'tradingagents.strategies.orchestration.cohort_orchestrator' not in sys.modules
assert 'tradingagents.strategies.orchestration.trading_calendar' not in sys.modules
assert 'pandas' not in sys.modules
''')


def test_strategy_exports_keep_their_public_names_and_class_identity():
    isolated('''
import importlib
import tradingagents.strategies as strategies
expected = {
 'Filter':'state.models','ScreenerCriteria':'state.models','BacktestResults':'state.models',
 'ScreenerResult':'state.models','Strategy':'state.models',
 'MultiStrategyEngine':'orchestration.multi_strategy_engine',
 'CohortOrchestrator':'orchestration.cohort_orchestrator','CohortConfig':'orchestration.cohort_orchestrator',
 'build_default_cohorts':'orchestration.cohort_orchestrator',
 'CohortComparison':'orchestration.cohort_comparison','SignalJournal':'learning.signal_journal'}
assert strategies.__all__ == list(expected)
assert set(expected) <= set(dir(strategies))
namespace = {}
exec('from tradingagents.strategies import *',namespace)
for name,suffix in expected.items():
 module = importlib.import_module('tradingagents.strategies.'+suffix)
 original = getattr(module,name)
 assert getattr(strategies,name) is original
 assert namespace[name] is original
 assert strategies.__dict__[name] is original
for name in ('state','orchestration','learning'):
 assert getattr(strategies,name) is importlib.import_module('tradingagents.strategies.'+name)
try:
 strategies.missing_public_export
except AttributeError as error:
 assert 'missing_public_export' in str(error)
else:
 raise AssertionError('unknown strategy export accepted')
''')
