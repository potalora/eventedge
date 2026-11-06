"""Parallel execution preserves candidate identity, context, and deadline evidence."""
from contextvars import ContextVar
from threading import Barrier, Event, Lock
from types import SimpleNamespace
import time

import pytest

from tradingagents.strategies.runtime_deadline import (
    ModelDeadlineExceeded, current_model_deadline, model_budget,
)


class LocalAnalyzer:
    def __init__(self):
        self.last_call_provenance = {}
        self.last_call_failure = ""

    def fork_for_parallel(self):
        return LocalAnalyzer()


def run(candidates, callback, **kwargs):
    from tradingagents.strategies.candidate_parallel import analyze_candidates
    return analyze_candidates(candidates, analyzer=kwargs.pop("analyzer", LocalAnalyzer()),
                              analyze_one=callback, **kwargs)


def test_parallel_results_keep_input_identity_despite_reverse_completion():
    first_can_finish = Event()
    candidates = [{"index": i} for i in range(3)]
    def analyze(candidate, analyzer):
        if candidate["index"] == 0:
            assert first_can_finish.wait(5)
        else:
            first_can_finish.set()
        analyzer.last_call_provenance["response_id"] = str(candidate["index"])
        candidate["response_id"] = analyzer.last_call_provenance["response_id"]
    outcomes = run(candidates, analyze, max_workers=2)
    assert [row.candidate for row in outcomes] == candidates
    assert all(row.candidate is candidates[i] for i, row in enumerate(outcomes))
    assert [row.candidate["response_id"] for row in outcomes] == ["0", "1", "2"]
    assert [row.failure for row in outcomes] == ["", "", ""]


def test_context_and_original_deadline_are_inherited_without_parent_mutation():
    marker = ContextVar("parallel_test_marker", default="outside")
    marker.set("original")
    parent = LocalAnalyzer()
    deadline = time.monotonic() + 300
    def analyze(candidate, analyzer):
        assert marker.get() == "original"
        assert current_model_deadline() == deadline
        assert analyzer is not parent
        marker.set(candidate["id"])
        analyzer.last_call_provenance["response_id"] = candidate["id"]
        candidate["provenance"] = dict(analyzer.last_call_provenance)
    with model_budget(deadline):
        outcomes = run([{"id": "a"}, {"id": "b"}], analyze, analyzer=parent, max_workers=2)
    assert [row.candidate["provenance"] for row in outcomes] == [{"response_id": "a"}, {"response_id": "b"}]
    assert marker.get() == "original"
    assert parent.last_call_provenance == {}
    assert current_model_deadline() is None


def test_explicit_worker_bound_and_all_inputs_are_processed():
    guard, barrier = Lock(), Barrier(3)
    counts = {"active": 0, "peak": 0, "calls": 0}
    def analyze(candidate, analyzer):
        with guard:
            counts["active"] += 1
            counts["calls"] += 1
            counts["peak"] = max(counts["peak"], counts["active"])
        if candidate < 3:
            barrier.wait(5)
        with guard:
            counts["active"] -= 1
    outcomes = run(list(range(19)), analyze, max_workers=3)
    assert counts == {"active": 0, "peak": 3, "calls": 19}
    assert [row.candidate for row in outcomes] == list(range(19))


def test_legacy_default_is_serial_and_uses_original_analyzer():
    parent, seen = LocalAnalyzer(), []
    def analyze(candidate, analyzer):
        assert analyzer is parent
        seen.append(candidate)
    assert [row.candidate for row in run([3, 1, 2], analyze, analyzer=parent)] == [3, 1, 2]
    assert seen == [3, 1, 2]


@pytest.mark.parametrize("workers", [0, -1, 65, 1.5, True, "2", None])
def test_invalid_worker_count_fails_before_processing(workers):
    seen = []
    with pytest.raises(ValueError, match="invalid_candidate_workers"):
        run([1], lambda candidate, analyzer: seen.append(candidate), max_workers=workers)
    assert seen == []


def test_callback_exception_retains_failed_input_and_later_inputs():
    def analyze(candidate, analyzer):
        if candidate == "bad":
            raise RuntimeError("private provider payload")
    outcomes = run(["first", "bad", "last"], analyze, max_workers=2)
    assert [(row.candidate, row.failure) for row in outcomes] == [
        ("first", ""), ("bad", "analysis_unavailable"), ("last", "")]
    assert "private" not in repr(outcomes)


def test_expired_budget_retains_every_candidate_without_calling_callback():
    seen = []
    with model_budget(time.monotonic() - 1):
        outcomes = run(list(range(7)), lambda candidate, analyzer: seen.append(candidate), max_workers=3)
    assert seen == []
    assert [(row.candidate, row.failure) for row in outcomes] == [
        (i, "model_deadline_exhausted") for i in range(7)]


def test_late_callback_result_and_queued_work_fail_without_prefix(monkeypatch):
    from tradingagents.strategies import runtime_deadline
    clock, seen = [0.0], []
    monkeypatch.setattr(runtime_deadline, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    def analyze(candidate, analyzer):
        seen.append(candidate)
        clock[0] = 11.0
    with model_budget(10):
        outcomes = run(list(range(6)), analyze, max_workers=1)
    assert seen == [0]
    assert [row.failure for row in outcomes] == ["model_deadline_exhausted"] * 6


def test_swallowed_individual_transport_timeout_is_an_explicit_failure():
    def analyze(candidate, analyzer):
        analyzer.last_call_failure = "model_deadline_exhausted"
    outcomes = run(["a", "b", "c"], analyze, max_workers=2)
    assert [row.failure for row in outcomes] == ["model_deadline_exhausted"] * 3


def test_each_candidate_gets_fresh_diagnostic_state_within_worker():
    def analyze(candidate, analyzer):
        assert analyzer.last_call_failure == ""
        assert analyzer.last_call_provenance == {}
        analyzer.last_call_failure = "old_failure"
        analyzer.last_call_provenance["response_id"] = candidate
    outcomes = run(list(range(9)), analyze, max_workers=2)
    assert [row.failure for row in outcomes] == [""] * 9


def test_analyzer_fork_copies_overrides_and_isolates_diagnostics_and_config():
    from openai import OpenAI
    from tradingagents.strategies.learning.llm_analyzer import LLMAnalyzer
    parent = LLMAnalyzer({"autoresearch": {"autoresearch_model": "gpt-6-luna",
                         "thesis_model": "gpt-6-astra", "llm_effort": "high",
                         "thesis_effort": "xhigh", "llm_temperature": .25}})
    parent.set_prompt_override("filing_analysis", "explicit system prompt")
    parent.last_call_provenance = {"response_id": "parent"}
    parent.last_call_failure = "parent_failure"
    client = OpenAI(api_key="offline-private", base_url="https://offline.invalid/v1",
                    organization="offline-org", project="offline-project",
                    default_headers={"X-Private-Setting": "retained"})
    parent._client = client
    parent._provider_clients["openai"] = client
    try:
        fork = parent.fork_for_parallel()
        assert fork._client is client and fork._provider_clients["openai"] is client
        assert fork._model_name == "gpt-6-luna" and fork._thesis_model == "gpt-6-astra"
        assert fork._effort == "high" and fork._thesis_effort == "xhigh"
        assert fork._temperature == .25
        assert fork.get_prompt("filing_analysis") == "explicit system prompt"
        assert fork.last_call_provenance == {} and fork.last_call_failure == ""
        fork.set_prompt_override("filing_analysis", "worker prompt")
        fork.config["autoresearch"]["llm_effort"] = "low"
        fork._provider_clients.clear()
        fork.last_call_provenance["response_id"] = "worker"
        assert parent.get_prompt("filing_analysis") == "explicit system prompt"
        assert parent.config["autoresearch"]["llm_effort"] == "high"
        assert parent._provider_clients["openai"] is client
        assert parent.last_call_provenance == {"response_id": "parent"}
    finally:
        client.close()


def test_parallel_native_transport_keeps_exact_client_settings_and_provenance(monkeypatch):
    from openai import OpenAI
    from tradingagents.strategies.learning.llm_analyzer import LLMAnalyzer
    parent = LLMAnalyzer({"autoresearch": {"autoresearch_model": "gpt-6-luna", "thesis_model": "gpt-6-astra"}})
    client = OpenAI(api_key="offline-private", base_url="https://offline.invalid/custom/v1",
                    organization="offline-org", project="offline-project",
                    default_headers={"X-Private-Setting": "retained"})
    parent._client = client
    barrier = Barrier(2)
    def transport(payload, timeout):
        assert payload["client"] == {"api_key": "offline-private", "base_url": "https://offline.invalid/custom/v1/",
            "organization": "offline-org", "project": "offline-project", "default_headers":
                {key: value for key, value in client.default_headers.items() if isinstance(value, str)}}
        assert 0 < timeout <= 120
        barrier.wait(5)
        identity = payload["request"]["prompt"]
        return {"text": identity, "provenance": {"response_id": identity}}
    monkeypatch.setattr("tradingagents.strategies.llm_utils.bounded_transport", transport)
    def analyze(candidate, analyzer):
        candidate["text"] = analyzer._call_llm("same system", candidate["id"])
        candidate["provenance"] = dict(analyzer.last_call_provenance)
    try:
        with model_budget(time.monotonic() + 300):
            outcomes = run([{"id": "one"}, {"id": "two"}], analyze, analyzer=parent, max_workers=2)
        assert [row.candidate["text"] for row in outcomes] == ["one", "two"]
        assert [row.candidate["provenance"]["response_id"] for row in outcomes] == ["one", "two"]
        assert parent.last_call_provenance == {}
    finally:
        client.close()


def test_fork_rejects_shared_unsupported_injected_client_before_dispatch():
    from tradingagents.strategies.learning.llm_analyzer import LLMAnalyzer
    parent = LLMAnalyzer()
    parent._client = SimpleNamespace(messages=object())
    seen = []
    with pytest.raises(ValueError, match="parallel_client_unsupported"):
        run([1, 2], lambda candidate, analyzer: seen.append(candidate), analyzer=parent, max_workers=2)
    assert seen == []


def test_explicit_injected_client_fork_is_respected():
    from tradingagents.strategies.learning.llm_analyzer import LLMAnalyzer
    class Injected:
        def __init__(self):
            self.state = []
        def fork_for_parallel(self):
            return Injected()
    parent = LLMAnalyzer()
    parent._client = Injected()
    clone = parent.fork_for_parallel()
    clone._client.state.append("worker")
    assert parent._client.state == []


def test_lazy_client_remains_worker_local_without_eager_unused_provider_init():
    from tradingagents.strategies.learning.llm_analyzer import LLMAnalyzer
    parent = LLMAnalyzer({"autoresearch": {"autoresearch_model": "gpt-6-luna", "thesis_model": "claude-sonnet-5"}})
    fork = parent.fork_for_parallel()
    assert fork._client is None and parent._client is None
    fork._provider_clients["anthropic"] = object()
    assert parent._provider_clients == {}


@pytest.mark.parametrize("failure", ["analysis_unavailable", "model_unavailable"])
def test_swallowed_model_failure_is_not_reported_as_success(failure):
    def analyze(candidate, analyzer):
        analyzer.last_call_failure = failure
    outcomes = run([1, 2], analyze, max_workers=2)
    assert [row.failure for row in outcomes] == ["analysis_unavailable", "analysis_unavailable"]


def test_parallel_deadline_accounts_for_running_and_never_dispatched_inputs(monkeypatch):
    from tradingagents.strategies import runtime_deadline
    clock, seen = [0.0], []
    barrier, advanced = Barrier(2), Event()
    monkeypatch.setattr(runtime_deadline, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    def analyze(candidate, analyzer):
        seen.append(candidate)
        barrier.wait(5)
        if candidate == 0:
            clock[0] = 11.0
            advanced.set()
        else:
            assert advanced.wait(5)
    with model_budget(10):
        outcomes = run(list(range(13)), analyze, max_workers=2)
    assert sorted(seen) == [0, 1]
    assert [row.candidate for row in outcomes] == list(range(13))
    assert [row.failure for row in outcomes] == ["model_deadline_exhausted"] * 13


def test_collection_after_deadline_cannot_return_previously_completed_success(monkeypatch):
    from tradingagents.strategies import candidate_parallel, runtime_deadline
    clock = [0.0]
    original_wait = candidate_parallel.wait
    monkeypatch.setattr(runtime_deadline, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    def late_collection(*args, **kwargs):
        result = original_wait(*args, **kwargs)
        clock[0] = 11.0
        return result
    monkeypatch.setattr(candidate_parallel, "wait", late_collection)
    with model_budget(10):
        outcomes = run([1, 2], lambda candidate, analyzer: None, max_workers=2)
    assert [row.failure for row in outcomes] == ["model_deadline_exhausted", "model_deadline_exhausted"]


def test_anthropic_existing_auth_and_mixed_provider_map_remain_exact():
    from anthropic import Anthropic
    from tradingagents.strategies.learning.llm_analyzer import LLMAnalyzer
    parent = LLMAnalyzer({"autoresearch": {"autoresearch_model": "gpt-6-luna", "thesis_model": "claude-sonnet-5"}})
    client = Anthropic(api_key="offline-key", auth_token="offline-auth", base_url="https://offline.invalid/custom",
                       default_headers={"X-Private-Setting": "retained"})
    parent._provider_clients["anthropic"] = client
    try:
        clone = parent.fork_for_parallel()
        assert clone._provider_clients["anthropic"] is client
        assert clone._provider_clients["anthropic"].api_key == "offline-key"
        assert clone._provider_clients["anthropic"].auth_token == "offline-auth"
        assert str(clone._provider_clients["anthropic"].base_url) == "https://offline.invalid/custom/"
        clone._provider_clients.clear()
        assert parent._provider_clients["anthropic"] is client
    finally:
        client.close()


def test_dispatch_never_queues_more_than_worker_bound(monkeypatch):
    from tradingagents.strategies import candidate_parallel
    original_executor, original_wait = candidate_parallel.ThreadPoolExecutor, candidate_parallel.wait
    released, first_wait = Event(), [True]
    counts = {"submitted": 0, "unfinished": 0, "peak": 0}
    lock = Lock()
    class ObservedExecutor(original_executor):
        def submit(self, *args, **kwargs):
            with lock:
                counts["submitted"] += 1
                counts["unfinished"] += 1
                counts["peak"] = max(counts["peak"], counts["unfinished"])
                assert counts["unfinished"] <= 3
            future = super().submit(*args, **kwargs)
            def finished(_):
                with lock:
                    counts["unfinished"] -= 1
            future.add_done_callback(finished)
            return future
    def collect(*args, **kwargs):
        if first_wait[0]:
            first_wait[0] = False
            assert counts["submitted"] == 3
            released.set()
        return original_wait(*args, **kwargs)
    def analyze(candidate, analyzer):
        assert released.wait(5)
    monkeypatch.setattr(candidate_parallel, "ThreadPoolExecutor", ObservedExecutor)
    monkeypatch.setattr(candidate_parallel, "wait", collect)
    outcomes = run(list(range(17)), analyze, max_workers=3)
    assert counts == {"submitted": 17, "unfinished": 0, "peak": 3}
    assert [row.failure for row in outcomes] == [""] * 17
