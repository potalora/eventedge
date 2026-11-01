"""The idle check observes the actual candidate calendar without replacing time."""
from datetime import datetime, timezone
import json
import sys
from types import SimpleNamespace

import pytest

from tools.live_workflow import bootstrap, runner


def test_idle_window_observes_real_calendar_in_exclusive_private_output(tmp_path):
    assert hasattr(bootstrap, '_idle_window')
    from tradingagents.strategies.orchestration import trading_calendar
    if not hasattr(trading_calendar, 'exchange_date'):
        pytest.skip('Standalone tooling checkout predates the candidate calendar API')
    exchange_date, is_session = trading_calendar.exchange_date, trading_calendar.is_session
    output = tmp_path / 'idle-observation'
    output.mkdir(mode=0o700)
    before = datetime.now(timezone.utc)
    bootstrap._idle_window(tmp_path, output)
    after = datetime.now(timezone.utc)
    value = json.loads((output / 'idle-window.json').read_text())
    observed = datetime.fromisoformat(value['observed_at'])
    assert before <= observed <= after
    assert value == {
        'observed_at': observed.isoformat(),
        'exchange_date': exchange_date(observed).isoformat(),
        'ordinary_idle_expected': not is_session(exchange_date(observed)),
    }
    with pytest.raises(FileExistsError):
        bootstrap._idle_window(tmp_path, output)


def test_idle_window_refuses_output_outside_campaign(tmp_path):
    assert hasattr(bootstrap, '_idle_window')
    campaign = tmp_path / 'campaign'
    campaign.mkdir(mode=0o700)
    output = tmp_path / 'outside'
    output.mkdir(mode=0o700)
    with pytest.raises(runner.AcceptanceError, match='idle_output_must_be_private_campaign_directory'):
        bootstrap._idle_window(campaign, output)
    assert list(output.iterdir()) == []


def test_idle_cli_has_no_credential_or_session_override(monkeypatch, capsys):
    seen = []
    def run_idle(args):
        seen.append(vars(args))
        return {'status': 'pass'}
    monkeypatch.setitem(sys.modules, 'tools.live_workflow.idle', SimpleNamespace(run_idle=run_idle))
    argv = ['idle', '--root', '/private/campaign', '--boundary-receipt', '/supervisor/boundary.json',
            '--attempt-id', 'timer-1']
    assert runner.main(argv) == 0
    assert seen == [{'operation': 'idle', 'root': '/private/campaign',
                     'boundary_receipt': '/supervisor/boundary.json', 'attempt_id': 'timer-1'}]
    for extra in (['--credential-env', 'ALPACA_API_KEY'], ['--session', '2026-11-02']):
        with pytest.raises(SystemExit) as error:
            runner.main(argv + extra)
        assert error.value.code == 2
        assert 'unrecognized arguments' in capsys.readouterr().err
    assert len(seen) == 1
