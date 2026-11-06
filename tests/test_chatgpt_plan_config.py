"""Optional subscription billing must not construct or reuse a paid client."""
from dataclasses import dataclass
from types import ModuleType, SimpleNamespace
import sys

import pytest


@pytest.fixture
def plan_boundary(monkeypatch):
    # The separately owned factory and auth boundary are controlled locally;
    # analyzer, committee, memo and epoch behavior remain the real application.
    module = ModuleType('tradingagents.strategies.chatgpt_plan_client')
    auth = ModuleType('tradingagents.strategies.plan_auth')
    @dataclass(frozen=True)
    class ChatGPTPlanClient:
        auth_path: str | None = '/private/profile.json'
        profile_identity: str = 'fixture-profile-identity'
        def close(self): pass
    current = {'identity': 'fixture-profile-identity'}
    def configured(config, model):
        ar = config.get('autoresearch', {})
        mode = ar.get('llm_billing_mode', 'api')
        if mode == 'api': return None
        if mode != 'chatgpt_plan' or model not in {'gpt-6-luna', 'gpt-6-astra', 'gpt-6.1-sol'}:
            raise ValueError('invalid_plan_configuration')
        return ChatGPTPlanClient(ar.get('chatgpt_plan_auth_path', '/private/profile.json'), current['identity'])
    def require(client, config, model):
        expected = configured(config, model)
        if expected is not None and (not isinstance(client, ChatGPTPlanClient) or client.profile_identity != current['identity']):
            raise ValueError('invalid_billing_client')
    module.ChatGPTPlanClient = ChatGPTPlanClient
    module.configured_plan_client = configured
    module.require_configured_billing = require
    auth.PlanAuthError = type("PlanAuthError", (RuntimeError,), {})
    auth.read_profile_identity = lambda path=None, **kwargs: current['identity']
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setitem(sys.modules, auth.__name__, auth)
    paid = []
    def forbidden(**kwargs):
        paid.append(kwargs)
        raise AssertionError('paid SDK must not be constructed')
    monkeypatch.setattr('openai.OpenAI', forbidden)
    monkeypatch.setattr('anthropic.Anthropic', forbidden)
    return module, current, paid


def plan_config(**changes):
    return {'autoresearch': {'llm_billing_mode': 'chatgpt_plan', 'autoresearch_model': 'gpt-6-luna',
        'thesis_model': 'gpt-6-astra', 'llm_effort': 'high', 'thesis_effort': 'high', **changes}}


@pytest.mark.parametrize('kind', ['analyzer', 'committee'])
def test_plan_lazy_initialization_uses_descriptor_without_paid_sdk(plan_boundary, kind):
    from tradingagents.strategies.learning.llm_analyzer import LLMAnalyzer
    from tradingagents.strategies.trading.portfolio_committee import PortfolioCommittee
    obj = LLMAnalyzer(plan_config()) if kind == 'analyzer' else PortfolioCommittee(plan_config())
    assert isinstance(obj._get_client(), plan_boundary[0].ChatGPTPlanClient)
    assert plan_boundary[2] == []


@pytest.mark.parametrize('kind', ['analyzer', 'committee'])
def test_injected_paid_client_cannot_cross_plan_call_boundary(plan_boundary, monkeypatch, kind):
    from tradingagents.strategies.learning.llm_analyzer import LLMAnalyzer
    from tradingagents.strategies.trading.portfolio_committee import PortfolioCommittee
    obj = LLMAnalyzer(plan_config()) if kind == 'analyzer' else PortfolioCommittee(plan_config())
    obj._client = SimpleNamespace()
    calls = []
    monkeypatch.setattr('tradingagents.strategies.llm_utils.call_analysis_model', lambda *a, **kw: calls.append(kw) or '{}')
    if kind == 'analyzer':
        assert obj._call_llm('sys', 'prompt') == ''
        assert obj.last_call_failure == 'analysis_unavailable'
    else:
        with pytest.raises(ValueError, match='invalid_billing_client'):
            obj._call_llm(system='sys', prompt='prompt')
    assert calls == []


def test_plan_descriptor_is_safe_to_share_in_parallel_fork(plan_boundary):
    from tradingagents.strategies.learning.llm_analyzer import LLMAnalyzer
    obj = LLMAnalyzer(plan_config())
    obj._client = plan_boundary[0].ChatGPTPlanClient()
    fork = obj.fork_for_parallel()
    assert fork._client is obj._client
    assert fork.last_call_provenance is not obj.last_call_provenance


def test_default_billing_mode_is_api():
    from tradingagents.default_config import DEFAULT_CONFIG
    assert DEFAULT_CONFIG['autoresearch']['llm_billing_mode'] == 'api'


@pytest.mark.parametrize('base,thesis,supported', [
    ('gpt-6-luna', 'claude-sonnet-5', False),
    ('claude-sonnet-5', 'gpt-6-astra', True),
])
def test_mixed_provider_role_never_falls_back_to_paid_sdk(plan_boundary, monkeypatch, base, thesis, supported):
    from tradingagents.strategies.learning.llm_analyzer import LLMAnalyzer
    obj = LLMAnalyzer(plan_config(autoresearch_model=base, thesis_model=thesis))
    clients = []
    monkeypatch.setattr('tradingagents.strategies.llm_utils.call_analysis_model', lambda client, **kw: clients.append(client) or '{}')
    assert obj._call_llm('s', 'p') == ('{}' if supported else '')
    assert plan_boundary[2] == []
    assert len(clients) == int(supported)
    if supported: assert isinstance(clients[0], plan_boundary[0].ChatGPTPlanClient)


@pytest.mark.parametrize('kind', ['analyzer', 'committee'])
def test_invalid_plan_mode_is_not_swallowed_into_paid_initialization(plan_boundary, kind):
    from tradingagents.strategies.learning.llm_analyzer import LLMAnalyzer
    from tradingagents.strategies.trading.portfolio_committee import PortfolioCommittee
    config = plan_config(llm_billing_mode='misspelled')
    obj = LLMAnalyzer(config) if kind == 'analyzer' else PortfolioCommittee(config)
    with pytest.raises(ValueError, match='invalid_plan_configuration'):
        obj._get_client()
    assert plan_boundary[2] == []


def test_plan_memo_reuses_only_current_profile_and_exact_request(plan_boundary):
    from tradingagents.strategies.candidate_response_reuse import (
        candidate_response_memo, begin_candidate_response, end_candidate_response,
        reused_candidate_response, retain_candidate_response, commit_candidate_response)
    client = plan_boundary[0].ChatGPTPlanClient()
    kwargs = dict(model='gpt-6-luna', system='s', prompt='p', max_tokens=10, temperature=0, effort='high', role='bounded')
    with candidate_response_memo():
        token = begin_candidate_response('filing', 'filing_change', 'discovery-fixture', False)
        try:
            assert reused_candidate_response(client, **kwargs) is None
            retain_candidate_response('{}', {'billing_mode': 'chatgpt_plan'})
            record = commit_candidate_response()
        finally: end_candidate_response(token)
        token = begin_candidate_response('filing', 'filing_change', 'discovery-fixture', False)
        try:
            reused = reused_candidate_response(client, **kwargs)
            assert reused[0] == '{}'
            assert reused[1]['request_reuse']['request_digest'] == record['request_digest']
            assert '/private' not in str(reused[1])
            assert reused_candidate_response(client, **{**kwargs, 'max_tokens': 11}) is None
            plan_boundary[1]['identity'] = 'replaced-profile-identity'
            assert reused_candidate_response(client, **kwargs) is None
            replacement = plan_boundary[0].ChatGPTPlanClient(profile_identity='replaced-profile-identity')
            assert reused_candidate_response(replacement, **kwargs) is None
        finally: end_candidate_response(token)


def test_plan_and_paid_client_memo_namespaces_cannot_collide(plan_boundary):
    from tradingagents.strategies.candidate_response_reuse import _Memo, _client_namespace
    class Paid:
        __module__ = 'openai'
        api_key, base_url, default_headers, organization, project = 'offline', 'https://offline.invalid', {}, None, None
    memo = _Memo()
    assert _client_namespace(memo, plan_boundary[0].ChatGPTPlanClient()) != _client_namespace(memo, Paid())


def test_metric_identity_distinguishes_only_explicit_plan_billing():
    from test_metric_epoch_runtime import _context, _models
    baseline = _context(models=_models())
    explicit_api = _context(models=_models(llm_billing_mode='api'))
    plan = _context(models=_models(llm_billing_mode='chatgpt_plan'))
    assert baseline.behavior_hash == explicit_api.behavior_hash
    assert baseline.config_hash == explicit_api.config_hash
    assert plan.behavior_hash != baseline.behavior_hash
    assert plan.config_hash == baseline.config_hash
    with pytest.raises(ValueError, match='invalid llm billing mode'):
        _context(models=_models(llm_billing_mode='misspelled'))


def test_committee_rechecks_plan_identity_before_each_attempt(plan_boundary, monkeypatch):
    from tradingagents.strategies.trading.portfolio_committee import PortfolioCommittee
    from tradingagents.strategies.runtime_deadline import ModelTransportError
    committee = PortfolioCommittee(plan_config())
    calls = []
    def reject(client, **kwargs):
        calls.append(client)
        plan_boundary[1]['identity'] = 'replacement'
        raise ModelTransportError('provider_error', 429)
    monkeypatch.setattr('tradingagents.strategies.llm_utils.call_analysis_model', reject)
    monkeypatch.setattr('tradingagents.strategies.runtime_deadline.model_backoff', lambda delay: None)
    with pytest.raises(ValueError, match='invalid_billing_client'):
        committee._call_llm(system='s', prompt='p')
    assert len(calls) == 1


def test_plan_memo_profile_read_uses_short_shared_deadline(plan_boundary, monkeypatch):
    import time
    from tradingagents.strategies.candidate_response_reuse import _Memo, _client_namespace
    from tradingagents.strategies.runtime_deadline import model_budget
    calls = []
    auth = sys.modules['tradingagents.strategies.plan_auth']
    def read(path=None, *, deadline=None):
        calls.append(deadline)
        return plan_boundary[1]['identity']
    monkeypatch.setattr(auth, 'read_profile_identity', read)
    deadline = time.monotonic() + .5
    with model_budget(deadline):
        assert _client_namespace(_Memo(), plan_boundary[0].ChatGPTPlanClient()) == 0
    assert calls == [deadline]
