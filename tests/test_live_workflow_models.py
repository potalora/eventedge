"""Model tape tests use the existing helper with one controlled native boundary."""
import importlib
import json
import time
from types import SimpleNamespace, ModuleType
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import sys

import pytest

from tools.live_workflow import recorded_models as models
from tools.live_workflow import recorded_io as tape


IDENTITY = {"candidate": "a" * 40, "profile_sha256": "b" * 64, "dependencies_sha256": "c" * 64}
REQUEST = dict(model="gpt-6-luna", system="system", prompt="private actual prompt", max_tokens=64,
               temperature=0.0, effort="high")
PROVENANCE = dict(configured_model="gpt-6-luna", returned_model="gpt-6-luna-revision",
                  returned_revision="revision-123", identity_status="pinned", response_id="resp-public",
                  reasoning_effort="high", billing_mode="chatgpt_plan", effective_output_limit=16384)


def contract_candidate_api(monkeypatch):
    """Minimal helper API fixture; this lane makes no native-integration claim.

    Only the transport primitive is controlled in native candidate tests. Here a
    test-local helper specifies the wrapper's call/observer/failure contract so CI
    for the independent tool does not require an unreleased candidate checkout.
    """
    package = importlib.import_module("tradingagents.strategies")
    runtime = ModuleType("tradingagents.strategies.runtime_deadline")
    class Deadline(TimeoutError):
        pass
    class Transport(RuntimeError):
        def __init__(self, reason, status_code=None, *, error_metadata=None):
            self.status_code, self.error_metadata = status_code, error_metadata
            super().__init__(reason)
    deadline = ContextVar("contract_model_deadline", default=None)
    @contextmanager
    def budget(value):
        token = deadline.set(value)
        try:
            yield
        finally:
            deadline.reset(token)
    def timeout():
        remaining = 120 if deadline.get() is None else min(120, deadline.get() - time.monotonic())
        if remaining <= 0:
            raise Deadline("model_deadline_exhausted")
        return remaining
    runtime.ModelDeadlineExceeded = Deadline
    runtime.ModelTransportError = Transport
    runtime.model_budget = budget
    runtime.model_timeout = timeout
    client_module = ModuleType("tradingagents.strategies.chatgpt_plan_client")
    @dataclass(frozen=True)
    class Client:
        auth_path: str
        profile_identity: str
    client_module.ChatGPTPlanClient = Client
    helper = ModuleType("tradingagents.strategies.llm_utils")
    def primitive(*args):
        pytest.fail("contract transport primitive was not supplied")
    helper.bounded_transport = primitive
    def call(client, *, model, system, prompt, max_tokens, temperature, effort, provenance=None,
             response_observer=None, document_inputs=None, max_call_seconds=None,
             regulation_graphics_model_policy=None):
        allowance = timeout()
        if provenance is not None:
            provenance.clear()
            provenance.update(configured_model=model, reasoning_effort=effort)
        result = helper.bounded_transport({"client": {"auth_path": client.auth_path},
                                          "request": {"model": model}}, allowance)
        if result.get("error"):
            raise Transport(result["error"], result.get("status_code"), error_metadata=result.get("error_metadata"))
        if provenance is not None:
            provenance.update(result["provenance"])
        if response_observer is not None:
            response_observer(result["text"])
        timeout()
        return result["text"]
    helper.call_analysis_model = call
    documents = ModuleType("tradingagents.strategies.plan_document_inputs")
    @dataclass(frozen=True)
    class Document:
        media_type: str
        data: bytes
        filename: str
        sha256: str
    documents.PlanDocumentInput = Document
    documents.encode_document_inputs = lambda inputs: inputs
    documents.document_input_metadata = lambda inputs: [dict(media_type=d.media_type, filename=d.filename,
                                                            sha256=d.sha256, size_bytes=len(d.data)) for d in inputs]
    analyzer = ModuleType("tradingagents.strategies.learning.llm_analyzer")
    committee = ModuleType("tradingagents.strategies.trading.portfolio_committee")
    class RoleOwner:
        def _call_llm(self, *args, **kwargs):
            raise AssertionError("contract role owner is not a native analyzer")
    analyzer.LLMAnalyzer = type("ContractAnalyzer", (RoleOwner,), {})
    committee.PortfolioCommittee = type("ContractCommittee", (RoleOwner,), {})
    for module in (runtime, client_module, helper, documents, analyzer, committee):
        monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(package, "llm_utils", helper, raising=False)
    return helper, Client, Deadline, budget


@pytest.fixture(autouse=True, params=["contract", "candidate"], ids=["contract", "candidate"])
def isolated_hooks(monkeypatch, request):
    global llm_utils, ChatGPTPlanClient, ModelDeadlineExceeded, model_budget
    models.uninstall_model_io()
    importlib.reload(tape)
    if request.param == "contract":
        llm_utils, ChatGPTPlanClient, ModelDeadlineExceeded, model_budget = contract_candidate_api(monkeypatch)
    else:
        client_module = pytest.importorskip("tradingagents.strategies.chatgpt_plan_client",
            reason="native candidate lane requires candidate first on PYTHONPATH; use pytest -k candidate")
        llm_utils = importlib.import_module("tradingagents.strategies.llm_utils")
        ChatGPTPlanClient = client_module.ChatGPTPlanClient
        runtime = importlib.import_module("tradingagents.strategies.runtime_deadline")
        ModelDeadlineExceeded, model_budget = runtime.ModelDeadlineExceeded, runtime.model_budget
    yield request.param
    models.uninstall_model_io()
    importlib.reload(tape)


def configure(tmp_path, mode="live_record", phase="native", identity=None):
    models.install_model_io(mode=mode, tape_dir=tmp_path, run_identity=identity or IDENTITY, phase=phase)


def restart(tmp_path, mode="offline_replay", **kwargs):
    models.uninstall_model_io()
    importlib.reload(tape)
    configure(tmp_path, mode=mode, **kwargs)


def native_client():
    return ChatGPTPlanClient(auth_path="/private/auth/NEVER-RECORD", profile_identity="public-profile")


def test_live_helper_and_existing_observer_and_provenance_survive(tmp_path, monkeypatch):
    received = []
    def primitive(payload, timeout):
        assert payload["client"]["auth_path"] == "/private/auth/NEVER-RECORD"
        assert payload["request"]["model"] == REQUEST["model"]
        assert 0 < timeout <= 120
        received.append("native")
        return {"text": '{"actual": true}', "provenance": PROVENANCE}
    monkeypatch.setattr(llm_utils, "bounded_transport", primitive)
    configure(tmp_path)
    provenance = {"role": "bounded"}
    observer = lambda text: received.append(text)
    assert llm_utils.call_analysis_model(native_client(), **REQUEST, provenance=provenance,
                                         response_observer=observer) == '{"actual": true}'
    assert received == ["native", '{"actual": true}']
    assert provenance == PROVENANCE
    all_bytes = b"".join(path.read_bytes() for path in tmp_path.rglob("*") if path.is_file())
    assert b"NEVER-RECORD" not in all_bytes
    assert b'"actual"' in all_bytes or b'\\"actual\\"' in all_bytes
    restart(tmp_path)
    monkeypatch.setattr(llm_utils, "bounded_transport", lambda *a: pytest.fail("replay contacted provider"))
    replayed = {"role": "bounded"}
    seen = []
    assert llm_utils.call_analysis_model(native_client(), **REQUEST, provenance=replayed,
                                         response_observer=seen.append) == '{"actual": true}'
    assert replayed == PROVENANCE
    assert seen == ['{"actual": true}']


def test_late_response_keeps_text_and_replays_deadline_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(llm_utils, "bounded_transport", lambda *a: {"text": "actual late text", "provenance": PROVENANCE})
    configure(tmp_path)
    seen = []
    def late_observer(text):
        seen.append(text)
        # Expire only after native transport delivered text, before the helper's final check.
        time.sleep(.025)
    with model_budget(time.monotonic() + .01), pytest.raises(ModelDeadlineExceeded):
        llm_utils.call_analysis_model(native_client(), **REQUEST, response_observer=late_observer)
    assert seen == ["actual late text"]
    restart(tmp_path)
    seen.clear()
    with pytest.raises(ModelDeadlineExceeded):
        llm_utils.call_analysis_model(native_client(), **REQUEST, response_observer=seen.append)
    assert seen == ["actual late text"]


@pytest.mark.parametrize("change", [{"prompt": "wrong prompt"}, {"model": "gpt-6.1-sol"}, {"effort": "low"}])
def test_replay_rejects_changed_request_without_provider(tmp_path, monkeypatch, change):
    monkeypatch.setattr(llm_utils, "bounded_transport", lambda *a: {"text": "native", "provenance": PROVENANCE})
    configure(tmp_path)
    llm_utils.call_analysis_model(native_client(), **REQUEST)
    restart(tmp_path)
    monkeypatch.setattr(llm_utils, "bounded_transport", lambda *a: pytest.fail("provider fallback"))
    with pytest.raises(Exception, match="(?i)(missing|mismatch|record|replay)"):
        llm_utils.call_analysis_model(native_client(), **(REQUEST | change))


def test_observer_failure_is_not_success_and_text_is_recorded(tmp_path, monkeypatch):
    monkeypatch.setattr(llm_utils, "bounded_transport", lambda *a: {"text": "received", "provenance": PROVENANCE})
    configure(tmp_path)
    def broken_observer(text):
        raise ValueError("NEVER-RECORD-secret-error")
    with pytest.raises(ValueError):
        llm_utils.call_analysis_model(native_client(), **REQUEST, response_observer=broken_observer)
    assert b"NEVER-RECORD" not in b"".join(p.read_bytes() for p in tmp_path.rglob("*") if p.is_file())
    restart(tmp_path)
    observed = []
    with pytest.raises(ValueError):
        llm_utils.call_analysis_model(native_client(), **REQUEST, response_observer=observed.append)
    assert observed == ["received"]


def test_document_request_and_public_provenance_replay_exactly(tmp_path, monkeypatch):
    import hashlib
    from tradingagents.strategies.plan_document_inputs import PlanDocumentInput
    data = b"\x89PNG\r\n\x1a\nprivate-image"
    doc = PlanDocumentInput(media_type="image/png", data=data, filename="figure.png", sha256=hashlib.sha256(data).hexdigest())
    metadata = [{"media_type": "image/png", "filename": "figure.png", "sha256": doc.sha256, "size_bytes": len(data)}]
    provenance = PROVENANCE | {"document_inputs": metadata}
    monkeypatch.setattr(llm_utils, "bounded_transport", lambda *a: {"text": "received", "provenance": provenance})
    configure(tmp_path)
    seen = {}
    llm_utils.call_analysis_model(native_client(), **REQUEST, document_inputs=[doc], provenance=seen)
    assert seen == provenance
    restart(tmp_path)
    seen.clear()
    llm_utils.call_analysis_model(native_client(), **REQUEST, document_inputs=[doc], provenance=seen)
    assert seen == provenance


@pytest.mark.parametrize("changed", ["phase", "identity", "client_profile", "occurrence"])
def test_replay_binding_is_exact(tmp_path, monkeypatch, changed):
    monkeypatch.setattr(llm_utils, "bounded_transport", lambda *a: {"text": "received", "provenance": PROVENANCE})
    configure(tmp_path)
    llm_utils.call_analysis_model(native_client(), **REQUEST)
    params = {"phase": "different"} if changed == "phase" else {"identity": IDENTITY | {"profile_sha256": "d" * 64}} if changed == "identity" else {}
    # Depending on tape identity validation, this can fail at configuration or at lookup.
    def try_replay():
        restart(tmp_path, **params)
        client = native_client()
        if changed == "client_profile":
            client = ChatGPTPlanClient(auth_path="/different/private/path", profile_identity="different-public-profile")
        if changed == "occurrence":
            assert llm_utils.call_analysis_model(client, **REQUEST) == "received"
        llm_utils.call_analysis_model(client, **REQUEST)
    monkeypatch.setattr(llm_utils, "bounded_transport", lambda *a: pytest.fail("provider fallback"))
    with pytest.raises(Exception, match="(?i)(missing|mismatch|record|replay|identity)"):
        try_replay()


def test_transport_error_typed_metadata_and_no_exception_text(tmp_path, monkeypatch):
    from tradingagents.strategies.runtime_deadline import ModelTransportError
    monkeypatch.setattr(llm_utils, "bounded_transport", lambda *a: {"error": "provider_error", "status_code": 429})
    configure(tmp_path)
    with pytest.raises(ModelTransportError) as live:
        llm_utils.call_analysis_model(native_client(), **REQUEST)
    restart(tmp_path)
    with pytest.raises(ModelTransportError) as replayed:
        llm_utils.call_analysis_model(native_client(), **REQUEST)
    assert replayed.value.status_code == live.value.status_code == 429
    assert replayed.value.error_metadata == live.value.error_metadata


@pytest.mark.parametrize("label", ["model_timeout", "model_refusal", "model_malformed_output", "model_late_response"])
def test_mutation_retains_immutable_parent_and_explicit_lane(tmp_path, monkeypatch, label):
    parent, mutated = tmp_path / "parent", tmp_path / "mutated"
    monkeypatch.setattr(llm_utils, "bounded_transport", lambda *a: {"text": '{"native": true}', "provenance": PROVENANCE})
    configure(parent)
    llm_utils.call_analysis_model(native_client(), **REQUEST)
    before = {str(p.relative_to(parent)): p.read_bytes() for p in parent.rglob("*") if p.is_file()}
    events = [json.loads(p.read_text()) for p in parent.rglob("*.json")]
    event = next(e for e in events if isinstance(e, dict) and e.get("boundary") == "model_helper" and "sdk_value" in e)
    models.create_model_mutation(parent_tape=parent, destination=mutated, event_id=event["event_id"], label=label)
    assert {str(p.relative_to(parent)): p.read_bytes() for p in parent.rglob("*") if p.is_file()} == before
    restart(mutated)
    seen = []
    if label == "model_malformed_output":
        text = llm_utils.call_analysis_model(native_client(), **REQUEST, response_observer=seen.append)
        with pytest.raises(json.JSONDecodeError):
            json.loads(text)
    else:
        with pytest.raises(RuntimeError if label == "model_refusal" else ModelDeadlineExceeded):
            llm_utils.call_analysis_model(native_client(), **REQUEST, response_observer=seen.append)
    if label == "model_late_response":
        assert seen == ['{"native": true}']
    elif label in ("model_timeout", "model_refusal"):
        assert seen == []
    changed = [json.loads(p.read_text()) for p in mutated.rglob("*.json")]
    assert any(e.get("representation") == "controlled_mutation" for e in changed if isinstance(e, dict))


def test_role_scopes_bind_identical_requests_separately(tmp_path, monkeypatch):
    monkeypatch.setattr(llm_utils, "bounded_transport", lambda *a: {"text": "received", "provenance": PROVENANCE})
    configure(tmp_path)
    def scoped_call(*, role="thesis"):
        return llm_utils.call_analysis_model(native_client(), **REQUEST)
    wrapped = models._wrap_role(scoped_call)
    for role in ("bounded", "thesis", "committee"):
        assert wrapped(role=role) == "received"
    events = tape.verify_tape(tmp_path)
    assert {event["public_request"]["role"] for event in events} == {"bounded", "thesis", "committee"}
    restart(tmp_path)
    for role in ("bounded", "thesis", "committee"):
        assert wrapped(role=role) == "received"
    with pytest.raises(Exception, match="missing"):
        llm_utils.call_analysis_model(native_client(), **REQUEST)


def test_native_role_owners_keep_their_call_paths(tmp_path, monkeypatch, isolated_hooks):
    if isolated_hooks != "candidate":
        pytest.skip("actual candidate analyzer/committee integration only")
    from tradingagents.strategies.learning.llm_analyzer import LLMAnalyzer
    from tradingagents.strategies.trading.portfolio_committee import PortfolioCommittee
    calls = []
    def direct(client, *, provenance, **request):
        calls.append(request["prompt"])
        provenance.update(PROVENANCE)
        return "native role text"
    monkeypatch.setattr(llm_utils, "_call_model_direct", direct)
    config = {"autoresearch": {"autoresearch_model": "gpt-6-luna", "llm_effort": "high"}}
    analyzer = LLMAnalyzer(config)
    committee = PortfolioCommittee(config)
    analyzer._client = committee._client = SimpleNamespace()
    configure(tmp_path)
    assert analyzer._call_llm("system", "prompt", 64, role="bounded") == "native role text"
    assert analyzer.last_call_response == {"received": True, "text": "native role text"}
    assert analyzer._call_llm("system", "prompt", 64, role="thesis") == "native role text"
    assert committee._call_llm(system="system", prompt="prompt", max_tokens=64) == "native role text"
    assert calls == ["prompt"] * 3
    assert {event["public_request"]["role"] for event in tape.verify_tape(tmp_path)} == {"bounded", "thesis", "committee"}


def test_replay_never_overrides_current_native_deadline(tmp_path, monkeypatch):
    monkeypatch.setattr(llm_utils, "bounded_transport", lambda *a: {"text": "received", "provenance": PROVENANCE})
    configure(tmp_path)
    llm_utils.call_analysis_model(native_client(), **REQUEST)
    restart(tmp_path)
    with model_budget(time.monotonic() - 1), pytest.raises(ModelDeadlineExceeded):
        llm_utils.call_analysis_model(native_client(), **REQUEST)


@pytest.mark.parametrize("change", [{"configured_model": "wrong-model"}, {"configured_model": None},
                                    {"reasoning_effort": "low"}, {"reasoning_effort": None}])
def test_invalid_replay_provenance_is_not_hidden_by_application_catch(tmp_path, monkeypatch, change):
    parent, changed = tmp_path / "parent", tmp_path / "changed"
    monkeypatch.setattr(llm_utils, "bounded_transport", lambda *a: {"text": "received", "provenance": PROVENANCE})
    configure(parent)
    llm_utils.call_analysis_model(native_client(), **REQUEST)
    event = tape.verify_tape(parent)[0]
    value = event["sdk_value"] | {"provenance": PROVENANCE | change}
    tape.create_mutation(parent_tape=parent, destination=changed, event_id=event["event_id"],
                         label="wrong_provenance", changes={"sdk_value": value})
    restart(changed)
    # Candidate analyzers convert generic helper exceptions into analysis_unavailable.
    try:
        llm_utils.call_analysis_model(native_client(), **REQUEST)
    except RuntimeError:
        pass
    else:
        pytest.fail("invalid model provenance was accepted")
    with pytest.raises(Exception, match="(?i)(fail|invalid|mismatch)"):
        tape.assert_replay_complete()


@pytest.mark.parametrize("interrupt_type", [KeyboardInterrupt, SystemExit, GeneratorExit])
@pytest.mark.parametrize("stage", ["transport", "observer"])
def test_native_and_replay_interruptions_preserve_baseexception_semantics(tmp_path, monkeypatch, interrupt_type, stage):
    interruption = interrupt_type(17) if interrupt_type is SystemExit else interrupt_type("NEVER-RECORD-interrupt")
    def primitive(*args):
        if stage == "transport":
            raise interruption
        return {"text": "received before interrupt", "provenance": PROVENANCE}
    def observer(text):
        raise interruption
    monkeypatch.setattr(llm_utils, "bounded_transport", primitive)
    configure(tmp_path)
    with pytest.raises(interrupt_type) as native:
        llm_utils.call_analysis_model(native_client(), **REQUEST, response_observer=observer)
    assert type(native.value) is interrupt_type
    restart(tmp_path)
    seen = []
    try:
        llm_utils.call_analysis_model(native_client(), **REQUEST, response_observer=seen.append)
    except Exception as exc:
        pytest.fail(f"candidate Exception handler could swallow replayed {interrupt_type.__name__}: {type(exc).__name__}")
    except BaseException as replayed:
        assert type(replayed) is type(native.value)
        if interrupt_type is SystemExit:
            assert replayed.code == native.value.code == 17
    else:
        pytest.fail("recorded interruption returned successfully")
    assert seen == (["received before interrupt"] if stage == "observer" else [])


@pytest.mark.parametrize("change,failure", [
    ({"response_observed": False, "received_text": None}, None),
    ({"received_text": "different observed text"}, None),
    ({"received_text": None}, None),
    ({"returned_text": None}, None),
    ({"response_observed": False}, None),
    ({"outcome": "raised"}, {"type": "RuntimeError", "reason": "model_call_failed"}),
    ({"outcome": "raised", "returned_text": None, "response_observed": False},
     {"type": "RuntimeError", "reason": "model_call_failed"}),
    ({"outcome": "raised", "returned_text": None}, None),
    ({}, {"type": "RuntimeError", "reason": "model_call_failed"}),
    ({"outcome": "raised", "returned_text": None}, {}),
    ({"outcome": "raised", "returned_text": None}, "not a typed failure"),
    ({"outcome": "raised", "returned_text": None}, {"type": [], "reason": "invalid"}),
    ({"outcome": "raised", "returned_text": None}, {"type": "SystemExit", "reason": "model_call_interrupted"}),
])
def test_impossible_model_envelopes_fail_sticky_before_observer(tmp_path, monkeypatch, change, failure):
    parent, changed = tmp_path / "parent", tmp_path / "changed"
    monkeypatch.setattr(llm_utils, "bounded_transport", lambda *a: {"text": "native text", "provenance": PROVENANCE})
    configure(parent)
    llm_utils.call_analysis_model(native_client(), **REQUEST)
    event = tape.verify_tape(parent)[0]
    value = event["sdk_value"] | change
    tape.create_mutation(parent_tape=parent, destination=changed, event_id=event["event_id"],
                         label="impossible_model_envelope", changes={"sdk_value": value, "failure": failure})
    restart(changed)
    seen = []
    provenance = {"sentinel": "untouched"}
    with pytest.raises(RuntimeError, match="invalid_recorded_model"):
        llm_utils.call_analysis_model(native_client(), **REQUEST, provenance=provenance, response_observer=seen.append)
    assert seen == []
    assert provenance == {"sentinel": "untouched"}
    with pytest.raises(Exception, match="(?i)(fail|invalid|mismatch)"):
        tape.assert_replay_complete()
