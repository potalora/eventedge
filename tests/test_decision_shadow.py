"""Clef is an optional evidence sidecar; it never adjudicates a trade."""
from copy import deepcopy
from pathlib import Path
import json
from types import SimpleNamespace

import pytest
import requests

from tradingagents.strategies.orchestration import decision_shadow as shadow

CREDS = {"CLOUDFLARE_ACCOUNT_ID": "a" * 32, "CLOUDFLARE_API_TOKEN": "test-token"}
CONFIG = {"enabled": True, "mode": "shadow"}


def response(payload=None, status=200):
    r = requests.Response()
    r.status_code = status
    r._content = json.dumps(payload or {
        "success": True, "result": {"model": "clef", "answers": {"support": {
            "type": "choice", "choice": "supported", "confidence": .8,
            "probabilities": {"supported": .8, "contradicted": .1, "insufficient": .1}}},
            "usage": {"input_tokens": 500, "output_tokens": 0}}}).encode()
    r._content_consumed = True
    return r


def inputs(n=1):
    signals = [{"event_key": f"docket-{i:02d}", "ticker": "NVDA", "strategy": "litigation",
        "direction": "short", "metadata": {"docket_id": i + 1,
            "llm_analysis": {"rationale": "Company faces a patent suit.", "evidence_claim": "Company faces a patent suit."},
            "cash_balance": "PRIVATE"}} for i in range(n)]
    data = {"courtlistener": {"dockets": [{"docket_id": i+1,
        "case_name": "Patent holder v NVDA", "date_filed": "2026-10-01",
        "nature_of_suit": "Patent", "cause": "Patent infringement", "secret": "PRIVATE"}
        for i in range(n)]}, "_execution_reference_bars": {"private": "PRIVATE"}}
    return signals, data


def run(tmp_path, *, signals=None, data=None, config=None, environ=None, post=None):
    if signals is None:
        signals, default_data = inputs()
        if data is None: data = default_data
    return shadow.evaluate_shadow(state_dir=tmp_path, generation="gen_018",
        session="2026-10-02", epoch_id="epoch-test", signals=signals, data=data or {},
        config=CONFIG if config is None else config, environ=CREDS if environ is None else environ,
        post=post or (lambda *a, **kw: response()))


def test_post_auth_schema_public_evidence_and_durable_result(tmp_path):
    captured = []
    def post(url, **kwargs):
        captured.append((url, kwargs))
        return response()
    signals, data = inputs()
    original = deepcopy((signals, data))
    result = run(tmp_path, signals=signals, data=data, post=post)
    entry = result["events"][0]
    assert entry["status"] == "ok" and entry["attempted"] is True
    assert entry["answer"]["probabilities"]["supported"] == .8
    assert entry["usage"] == {"input_tokens": 500, "output_tokens": 0}
    assert captured[0][0] == "https://api.cloudflare.com/client/v4/accounts/" + "a"*32 + "/ai/run/@cf/cloudflare/clef"
    assert captured[0][1]["headers"]["Authorization"] == "Bearer test-token"
    assert captured[0][1]["json"]["model"] == "clef"
    assert set(captured[0][1]["json"]["questions"]["support"]["criteria"]) == {"supported", "contradicted", "insufficient"}
    serialized = json.dumps(result)
    assert "PRIVATE" not in serialized and "test-token" not in serialized
    assert "Patent holder" in serialized and "untrusted" in serialized
    assert (signals, data) == original
    assert json.loads((tmp_path / "decision_shadow/2026-10-02.json").read_text()) == result


def test_missing_credentials_can_retry_saved_public_inputs_without_refetch(tmp_path):
    first = run(tmp_path, environ={})
    assert first["status"] == "missing_credentials"
    assert first["events"][0]["attempted"] is False
    second = run(tmp_path, signals=[], data={})
    assert second["events"][0]["status"] == "ok"
    assert second["events"][0]["input_hash"] == first["events"][0]["input_hash"]


def test_attempted_entries_and_accepted_answers_are_never_recalled_or_overwritten(tmp_path):
    first = run(tmp_path)
    def forbidden(*a, **k):
        pytest.fail("resume attempted a paid call")
    second = run(tmp_path, post=forbidden)
    assert second == first


@pytest.mark.parametrize("fault", ["timeout", "http", "malformed", "nonfinite", "missing_option", "wrong_model", "bad_sum", "bad_confidence", "negative_usage", "wrong_type", "extra_question", "large"])
def test_api_fault_is_durable_sanitized_and_never_retried(tmp_path, fault):
    def post(*a, **kw):
        if fault == "timeout":
            raise requests.Timeout("PRIVATE test-token")
        if fault == "http":
            return response({"error": "PRIVATE"}, 403)
        r = response()
        payload = r.json()
        answer = payload["result"]["answers"]["support"]
        if fault == "malformed":
            r._content = b'not-json PRIVATE'
            return r
        if fault == "nonfinite": answer["probabilities"]["supported"] = float("nan")
        if fault == "missing_option": del answer["probabilities"]["insufficient"]
        if fault == "wrong_model": payload["result"]["model"] = "untrusted-model"
        if fault == "bad_sum": answer["probabilities"]["supported"] = .3
        if fault == "bad_confidence": answer["confidence"] = 1.1
        if fault == "negative_usage": payload["result"]["usage"]["input_tokens"] = -2
        if fault == "wrong_type": answer["type"] = "noul"
        if fault == "extra_question": payload["result"]["answers"]["private"] = "PRIVATE"
        if fault == "large": payload["result"]["private"] = "x" * 100_000
        return response(payload)
    first = run(tmp_path, post=post)
    assert first["events"][0]["status"] in {"timeout", "http_error", "invalid_response"}
    assert "PRIVATE" not in json.dumps(first) and "test-token" not in json.dumps(first)
    assert run(tmp_path, post=lambda *a, **k: pytest.fail("retry")) == first


@pytest.mark.parametrize("damage", ["not-json", '{"schema_version":1}', "large", "probability"])
def test_corrupt_sidecar_is_unavailable_preserved_and_not_replaced(tmp_path, damage):
    run(tmp_path)
    p = tmp_path / "decision_shadow/2026-10-02.json"
    if damage == "probability":
        obj = json.loads(p.read_text()); obj["events"][0]["answer"]["probabilities"]["supported"] = -1
        p.write_text(json.dumps(obj))
    else: p.write_text("x" * 200_000 if damage == "large" else damage)
    before = p.read_bytes()
    assert run(tmp_path, post=lambda *a, **k: pytest.fail("call on corruption"))["status"] == "unavailable"
    assert p.read_bytes() == before


def test_unique_deterministic_cap_and_explicit_truncation(tmp_path):
    signals, data = inputs(8)
    result = run(tmp_path, signals=list(reversed(signals)) + signals, data=data)
    assert [e["event_key"] for e in result["events"]] == [f"docket-{i:02d}" for i in range(5)]
    assert result["skipped_events"] == 3
    assert result["selection_truncated"] is True


def test_generated_claim_is_not_treated_as_source_evidence(tmp_path):
    result = run(tmp_path, data={}, post=lambda *a, **k: pytest.fail("self-verification"))
    assert result["events"][0]["status"] == "insufficient_evidence"
    assert result["events"][0]["attempted"] is False


def test_large_original_source_is_bounded_and_truncation_visible(tmp_path):
    signals, data = inputs()
    data["courtlistener"]["dockets"][0]["cause"] = "é" * 100_000
    result = run(tmp_path, signals=signals, data=data)
    entry = result["events"][0]
    assert entry["evidence_truncated"] is True
    assert len(json.dumps(entry["input"], ensure_ascii=False).encode()) < 12_000


def test_budget_exhaustion_skips_unattempted_events(tmp_path):
    signals, data = inputs(3)
    clock = iter([0, 0, 0, 30, 30, 30, 30, 30])
    result = shadow.evaluate_shadow(state_dir=tmp_path, generation="gen_018", session="2026-10-02",
        epoch_id="epoch-test", signals=signals, data=data, config=CONFIG,
        environ=CREDS, post=lambda *a, **k: response(), monotonic=lambda: next(clock))
    assert result["events"][0]["status"] == "ok"
    assert result["events"][1]["status"] == "budget_exhausted"
    assert result["events"][1]["attempted"] is False


def test_disabled_mode_has_no_network_or_sidecar(tmp_path):
    result = run(tmp_path, config={"enabled": False}, post=lambda *a, **k: pytest.fail("disabled"))
    assert result["status"] == "disabled"
    assert not (tmp_path / "decision_shadow").exists()


@pytest.mark.parametrize('alter', ['private_input', 'attempt_flag', 'bad_latency', 'unknown_status'])
def test_semantically_corrupt_sidecar_cannot_send_or_reuse_unvalidated_values(tmp_path, alter):
    run(tmp_path, environ={})
    p = tmp_path / 'decision_shadow/2026-10-02.json'
    obj = json.loads(p.read_text())
    if alter == 'private_input':
        obj['events'][0]['input']['private_balance'] = 'PRIVATE'
        import hashlib
        obj['events'][0]['input_hash'] = hashlib.sha256(json.dumps(obj['events'][0]['input'], sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
    if alter == 'attempt_flag': obj['events'][0]['attempted'] = True
    if alter == 'bad_latency': obj['events'][0]['latency_ms'] = -1
    if alter == 'unknown_status': obj['status'] = 'accepted'
    p.write_text(json.dumps(obj))
    assert run(tmp_path)['status'] == 'unavailable'


def test_slow_transport_cannot_hold_daily_pipeline_past_request_budget(tmp_path):
    import time
    release = __import__('threading').Event()
    def post(*a, **kw):
        release.wait(.5)
        return response()
    started = time.monotonic()
    result = run(tmp_path, config={**CONFIG, 'request_timeout_seconds': .02}, post=post)
    elapsed = time.monotonic() - started
    release.set()
    assert elapsed < .3
    assert result['events'][0]['status'] == 'timeout'
    assert run(tmp_path, post=lambda *a, **kw: response()) == result


def test_shadow_switch_and_credentials_do_not_change_source_input_identity(monkeypatch):
    from tradingagents.strategies.orchestration.source_inputs import source_configuration_fingerprint
    base = {'autoresearch': {'state_dir': '/tmp/generation'}, 'llm_provider': 'openai'}
    before = source_configuration_fingerprint(base)
    monkeypatch.setenv('CLOUDFLARE_ACCOUNT_ID', 'a'*32)
    monkeypatch.setenv('CLOUDFLARE_API_TOKEN', 'test-token')
    assert source_configuration_fingerprint({**base, 'decision_shadow': {'enabled': True}}) == before
    assert source_configuration_fingerprint({**base, 'decision_shadow': {'enabled': False}}) == before


def test_saved_retry_command_reads_only_sidecar_and_returns_compact_status(tmp_path, monkeypatch, capsys):
    from scripts import run_decision_shadow
    run(tmp_path, environ={})
    for key, value in CREDS.items(): monkeypatch.setenv(key, value)
    monkeypatch.setattr(shadow.requests, 'post', lambda *a, **k: response())
    code = run_decision_shadow.main(['--state-dir', str(tmp_path), '--generation', 'gen_018',
        '--date', '2026-10-02', '--epoch-id', 'epoch-test'])
    assert code == 0
    result = json.loads(capsys.readouterr().out)
    assert result['event_counts'] == {'ok': 1}
    assert 'test-token' not in json.dumps(result) and 'claim' not in result


def test_saved_retry_command_does_not_create_missing_sidecar(tmp_path, capsys):
    from scripts import run_decision_shadow
    assert run_decision_shadow.main(['--state-dir', str(tmp_path), '--generation', 'gen_018',
        '--date', '2026-10-02', '--epoch-id', 'epoch-test']) == 2
    assert not (tmp_path / 'decision_shadow').exists()


def test_expected_commit_mismatch_makes_sidecar_unavailable_without_overwrite(tmp_path):
    run(tmp_path)
    path = tmp_path / 'decision_shadow/2026-10-02.json'
    assert shadow.read_shadow_summary(path, generation='gen_018', session='2026-10-02',
                                      generation_commit='wrong')['status'] == 'unavailable'


def test_resumed_missing_horizon_scope_is_explicit_and_never_samples_a_subset(tmp_path):
    owner = SimpleNamespace(_base_config={'autoresearch': {'state_dir': str(tmp_path)},
        'decision_shadow': CONFIG}, _metric_epoch_context=SimpleNamespace(
        generation_id='gen_018', generation_commit='b'*40), cohorts=[
            {'config': SimpleNamespace(horizon='30d')}, {'config': SimpleNamespace(horizon='3m')}])
    signals, data = inputs()
    state = SimpleNamespace(owner=owner, trading_date='2026-10-02', epoch_id='epoch-test',
                            horizon_signals={'3m': (signals, {}, [])}, shared_data=data)
    result = shadow.run_decision_shadow(state)
    assert result['status'] == 'incomplete_sampling' and result['events'] == []
    path = tmp_path / 'decision_shadow/2026-10-02.json'
    assert shadow.read_shadow_summary(path, generation='gen_018', session='2026-10-02')['status'] == 'incomplete_sampling'


def test_deeply_nested_corrupt_sidecar_is_unavailable_and_preserved(tmp_path):
    path = tmp_path / 'decision_shadow/2026-10-02.json'
    path.parent.mkdir()
    payload = '[' * 1200 + '0' + ']' * 1200
    path.write_text(payload)
    result = shadow.read_shadow_summary(path, generation='gen_018', session='2026-10-02')
    assert result == {'status': 'unavailable', 'events': []}
    assert path.read_text() == payload


def test_hosted_clef_confidence_can_differ_from_selected_probability(tmp_path):
    """Captured Cloudflare HTTP200 response: confidence is an independent value."""
    payload = {
        'result': {
            'model': 'clef',
            'answers': {'support': {
                'type': 'choice', 'choice': 'supported', 'confidence': 0.932,
                'probabilities': {'supported': 0.9769, 'contradicted': 0.0116,
                                  'insufficient': 0.0115}}},
            'usage': {'input_tokens': 252, 'output_tokens': 0}},
        'success': True, 'errors': [], 'messages': []}
    result = run(tmp_path, post=lambda *a, **kw: response(payload))
    entry = result['events'][0]
    assert entry['status'] == 'ok'
    assert entry['answer']['confidence'] == 0.932
    assert entry['answer']['probabilities']['supported'] == 0.9769
    assert entry['usage'] == {'input_tokens': 252, 'output_tokens': 0}
    assert run(tmp_path, post=lambda *a, **kw: pytest.fail('repeat paid call')) == result


@pytest.mark.parametrize('confidence', [0.0, 0.1, 1.0])
def test_valid_independent_confidence_does_not_change_selected_choice(tmp_path, confidence):
    payload = response().json()
    payload['result']['answers']['support']['confidence'] = confidence
    result = run(tmp_path, post=lambda *a, **kw: response(payload))
    assert result['events'][0]['status'] == 'ok'
    assert result['events'][0]['answer']['choice'] == 'supported'


@pytest.mark.parametrize('confidence', [float('nan'), float('inf'), -0.1, 1.1, True])
def test_independent_confidence_must_remain_a_finite_probability(tmp_path, confidence):
    payload = response().json()
    payload['result']['answers']['support']['confidence'] = confidence
    result = run(tmp_path, post=lambda *a, **kw: response(payload))
    assert result['events'][0]['status'] == 'invalid_response'


def test_choice_must_still_match_highest_option_probability(tmp_path):
    payload = response().json()
    payload['result']['answers']['support'].update(choice='contradicted', confidence=0.8)
    result = run(tmp_path, post=lambda *a, **kw: response(payload))
    assert result['events'][0]['status'] == 'invalid_response'
