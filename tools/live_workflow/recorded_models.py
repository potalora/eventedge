"""Private tapes around the native model helper, before application parsing.

Live mode delegates transport, validation, deadlines and observers to the candidate.
Replay has no provider fallback. Client/auth settings never enter a request record.
"""
from __future__ import annotations

from contextvars import ContextVar
from functools import wraps
import inspect

from . import recorded_io as tape

_BOUNDARY = "model_helper"
_OPERATION = "call_analysis_model"
_ROLE = ContextVar("live_workflow_model_role", default=None)
_PATCHES = []
_PROVENANCE_FIELDS = frozenset({
    "configured_model", "returned_model", "returned_revision", "identity_status", "response_id",
    "reasoning_effort", "billing_mode", "effective_output_limit", "role",
    "regulation_graphics_model_policy",
})


def _public_provenance(value):
    result = {key: item for key, item in value.items() if key in _PROVENANCE_FIELDS
              and (item is None or type(item) in (str, int, float, bool))}
    documents = value.get("document_inputs")
    if isinstance(documents, list):
        allowed = {"media_type", "filename", "sha256", "size_bytes", "presentation_policy"}
        result["document_inputs"] = [{key: item for key, item in document.items()
                                      if key in allowed and type(item) in (str, int)}
                                     for document in documents if isinstance(document, dict)]
    return result


def _public_request(arguments):
    request = {key: arguments[key] for key in (
        "model", "system", "prompt", "max_tokens", "temperature", "effort", "max_call_seconds",
        "regulation_graphics_model_policy")}
    scope = _ROLE.get()
    request["role"] = scope or arguments["provenance"].get("role")
    client = arguments["client"]
    # This public stable identity is distinct from the native auth path/grant.
    from tradingagents.strategies.chatgpt_plan_client import ChatGPTPlanClient
    request["client_profile_identity"] = client.profile_identity if isinstance(client, ChatGPTPlanClient) else None
    bases = {base.__module__.split(".")[0] for base in type(client).__mro__}
    request["native_provider"] = ("chatgpt_plan" if isinstance(client, ChatGPTPlanClient)
                                  else "openai" if "openai" in bases else "anthropic" if "anthropic" in bases
                                  else "injected")
    documents = arguments["document_inputs"]
    if documents is not None:
        from tradingagents.strategies.plan_document_inputs import encode_document_inputs, document_input_metadata
        request["document_inputs"] = document_input_metadata(encode_document_inputs(documents))
    else:
        request["document_inputs"] = None
    return request


def _public_failure(exc, observer_failed=False):
    from tradingagents.strategies.runtime_deadline import ModelDeadlineExceeded, ModelTransportError
    if isinstance(exc, ModelDeadlineExceeded):
        kind, reason = "ModelDeadlineExceeded", "model_deadline_exhausted"
    elif isinstance(exc, ModelTransportError):
        kind, reason = "ModelTransportError", "provider_error"
    elif isinstance(exc, (KeyboardInterrupt, SystemExit, GeneratorExit)):
        kind, reason = type(exc).__name__, "model_call_interrupted"
    elif not isinstance(exc, Exception):
        kind, reason = "BaseException", "model_call_interrupted"
    else:
        kind = type(exc).__name__ if type(exc) in (ValueError, RuntimeError, TimeoutError, TypeError) else "RuntimeError"
        reason = "response_observer_failed" if observer_failed else "model_call_failed"
        # Match known candidate strings only; provider exception text is private.
        if type(exc) is RuntimeError and exc.args == ("OpenAI analysis response was refused",):
            reason = "model_response_refused"
    result = {"type": kind, "reason": reason}
    if isinstance(exc, SystemExit):
        result["exit_code"] = exc.code if exc.code is None or type(exc.code) is int else 1
    if isinstance(exc, ModelTransportError):
        result.update(status_code=exc.status_code, error_metadata=exc.error_metadata)
    return result


def _invalid_replay(reason):
    tape.record_replay_failure(reason)
    raise RuntimeError(reason)


def _raise_recorded(failure):
    from tradingagents.strategies.runtime_deadline import ModelDeadlineExceeded, ModelTransportError
    if not isinstance(failure, dict):
        _invalid_replay("invalid_recorded_model_failure")
    kind, reason = failure.get("type"), failure.get("reason", "recorded_model_failure")
    if kind == "SystemExit":
        raise SystemExit(failure.get("exit_code", 1))
    if kind == "ModelTransportError":
        raise ModelTransportError(reason, failure.get("status_code"), error_metadata=failure.get("error_metadata"))
    classes = {"ModelDeadlineExceeded": ModelDeadlineExceeded, "ValueError": ValueError,
               "RuntimeError": RuntimeError, "TimeoutError": TimeoutError,
               "TypeError": TypeError, "InterruptedError": InterruptedError,
               "KeyboardInterrupt": KeyboardInterrupt, "GeneratorExit": GeneratorExit,
               "BaseException": BaseException}
    if kind not in classes:
        _invalid_replay("invalid_recorded_model_failure")
    raise classes[kind](reason)


def _validate_replay_outcome(value, failure):
    """Reject impossible native transitions before changing caller state."""
    fields = {"outcome", "received_text", "returned_text", "response_observed", "provenance"}
    if (not isinstance(value, dict) or not fields <= value.keys()
            or value["outcome"] not in ("returned", "raised")
            or type(value["response_observed"]) is not bool
            or not isinstance(value["provenance"], dict)):
        _invalid_replay("invalid_recorded_model_outcome")
    if ((value["response_observed"] and type(value["received_text"]) is not str)
            or (not value["response_observed"] and value["received_text"] is not None)):
        _invalid_replay("invalid_recorded_model_text")
    if value["outcome"] == "returned":
        if (failure is not None or not value["response_observed"]
                or type(value["returned_text"]) is not str
                or value["returned_text"] != value["received_text"]):
            _invalid_replay("invalid_recorded_model_outcome")
    elif value["returned_text"] is not None or not isinstance(failure, dict):
        _invalid_replay("invalid_recorded_model_outcome")
    if failure is not None:
        allowed = {"ModelDeadlineExceeded", "ModelTransportError", "ValueError", "RuntimeError",
                   "TimeoutError", "TypeError", "InterruptedError", "KeyboardInterrupt",
                   "SystemExit", "GeneratorExit", "BaseException"}
        if (type(failure.get("type")) is not str or failure["type"] not in allowed
                or type(failure.get("reason")) is not str
                or (failure["type"] == "SystemExit" and ("exit_code" not in failure
                    or (failure["exit_code"] is not None and type(failure["exit_code"]) is not int)))):
            _invalid_replay("invalid_recorded_model_failure")


def _wrap_model(original, mode):
    signature = inspect.signature(original)

    @wraps(original)
    def recorded(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        arguments = bound.arguments
        # A private destination also captures provenance when a caller does not request it.
        if arguments["provenance"] is None:
            arguments["provenance"] = {}
        public_request = _public_request(arguments)
        if mode == "offline_replay":
            from tradingagents.strategies.runtime_deadline import model_timeout
            model_timeout()
            event = tape.replay_event(boundary=_BOUNDARY, operation=_OPERATION, public_request=public_request)
            value = event.get("sdk_value")
            _validate_replay_outcome(value, event.get("failure"))
            provenance = value["provenance"]
            allow_unreceived = value["outcome"] == "raised" and not value["response_observed"]
            for field, expected in (("configured_model", public_request["model"]),
                                    ("reasoning_effort", public_request["effort"])):
                if provenance.get(field) != expected and not (allow_unreceived and provenance.get(field) is None):
                    _invalid_replay("recorded_model_provenance_mismatch")
            arguments["provenance"].clear()
            arguments["provenance"].update(provenance)
            if value["response_observed"]:
                if arguments["response_observer"] is not None:
                    arguments["response_observer"](value["received_text"])
            if event.get("failure") is not None:
                _raise_recorded(event["failure"])
            model_timeout()
            return value["returned_text"]

        event = tape.reserve_event(boundary=_BOUNDARY, operation=_OPERATION, public_request=public_request)
        observer = arguments["response_observer"]
        value = {"received_text": None, "returned_text": None, "response_observed": False,
                 "outcome": "raised", "provenance": {}}
        observer_failed = False

        def observed(text):
            nonlocal observer_failed
            value.update(received_text=text, response_observed=True)
            if observer is not None:
                try:
                    observer(text)
                except BaseException:
                    observer_failed = True
                    raise

        arguments["response_observer"] = observed
        try:
            text = original(*bound.args, **bound.kwargs)
        except BaseException as exc:
            value["provenance"] = _public_provenance(arguments["provenance"])
            tape.finish_event(event, sdk_value=value, failure=_public_failure(exc, observer_failed))
            raise
        value.update(returned_text=text, outcome="returned", provenance=_public_provenance(arguments["provenance"]))
        tape.finish_event(event, sdk_value=value)
        return text
    return recorded


def _wrap_role(original, role=None):
    signature = inspect.signature(original)

    @wraps(original)
    def scoped(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        token = _ROLE.set(role or bound.arguments.get("role", "thesis"))
        try:
            return original(*args, **kwargs)
        finally:
            _ROLE.reset(token)
    return scoped


def install_model_io(*, mode, tape_dir, run_identity, phase):
    """Install once per process after the exact candidate is on sys.path."""
    tape.configure_tape(mode=mode, tape_dir=tape_dir, run_identity=run_identity, phase=phase)
    if _PATCHES:
        return
    from tradingagents.strategies import llm_utils
    from tradingagents.strategies.learning.llm_analyzer import LLMAnalyzer
    from tradingagents.strategies.trading.portfolio_committee import PortfolioCommittee
    for owner, name, replacement in (
        (llm_utils, "call_analysis_model", _wrap_model(llm_utils.call_analysis_model, mode)),
        (LLMAnalyzer, "_call_llm", _wrap_role(LLMAnalyzer._call_llm)),
        (PortfolioCommittee, "_call_llm", _wrap_role(PortfolioCommittee._call_llm, "committee")),
    ):
        _PATCHES.append((owner, name, getattr(owner, name)))
        setattr(owner, name, replacement)


def uninstall_model_io():
    """Restore process-local hooks, primarily for bounded test processes."""
    while _PATCHES:
        owner, name, original = _PATCHES.pop()
        setattr(owner, name, original)


def create_model_mutation(*, parent_tape, destination, event_id, label):
    """Derive explicit controlled outcomes; never rewrite the native parent tape."""
    records = tape.verify_tape(parent_tape)
    matches = [event for event in records if isinstance(event, dict) and event.get("event_id") == event_id
               and event.get("boundary") == _BOUNDARY and "sdk_value" in event]
    if len(matches) != 1:
        raise ValueError("missing_or_ambiguous_parent_model_event")
    value = dict(matches[0]["sdk_value"])
    if label == "model_malformed_output":
        value.update(outcome="returned", returned_text="{", received_text="{", response_observed=True)
        failure = None
    elif label in ("model_timeout", "model_refusal", "model_late_response"):
        value.update(outcome="raised", returned_text=None)
        if label == "model_late_response":
            if not value.get("response_observed") or type(value.get("received_text")) is not str:
                raise ValueError("late_mutation_requires_received_text")
        else:
            value.update(received_text=None, response_observed=False)
        failure = ({"type": "RuntimeError", "reason": "model_response_refused"} if label == "model_refusal"
                   else {"type": "ModelDeadlineExceeded", "reason": "model_deadline_exhausted"})
    else:
        raise ValueError("unsupported_model_mutation")
    return tape.create_mutation(parent_tape=parent_tape, destination=destination, event_id=event_id,
                                label=label, changes={"sdk_value": value, "failure": failure})
