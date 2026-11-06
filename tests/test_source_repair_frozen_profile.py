"""Repair export corroborates accepted sources using exact frozen profile bytes."""

import hashlib
import json
from pathlib import Path

import pytest

from test_source_repair import NOW, evidence as evidence, pair
from tradingagents.strategies.orchestration import source_repair as contract


def bind_profile(evidence, *, declared_filing=False, relative=False):
    root, old_request, source, manifest, output = evidence
    profile = (Path(__file__).parents[1] / "config/profiles/stabilization-v2.json").read_bytes() if declared_filing else b'{"autoresearch":{}}\n'
    config_path = root / "data/generations/gen_020/generation-config.json"
    config_path.write_bytes(profile)
    digest = hashlib.sha256(profile).hexdigest()
    metadata = json.loads(manifest.read_bytes())
    metadata["generations"][0].update(
        config_file=str(config_path.relative_to(root) if relative else config_path),
        config_sha256=digest,
    )
    manifest.write_text(json.dumps(metadata))
    if declared_filing:
        from tradingagents.strategies.orchestration import filing_research_policy, source_lifecycle_policy
        from tradingagents.strategies.orchestration.operating_contract import profile_contract, configure_strategies
        from tradingagents.strategies.modules import get_paper_trade_strategies
        from tradingagents.strategies.orchestration.source_inputs import SourceInputStore

        operating = profile_contract(json.loads(profile), profile_sha256=digest)
        ar = operating.config["autoresearch"]
        for path in source.parent.glob("*.json"):
            envelope = SourceInputStore.decode(path.read_text())
            payload = envelope["payload"]
            payload.update(filing_research_policy.declaration(ar))
            payload.update(source_lifecycle_policy.declaration(ar, configure_strategies(get_paper_trade_strategies(), ar)))
            payload["edgar"] = filing_research_policy.sentinel()
            envelope["digest"] = hashlib.sha256(SourceInputStore.encode(payload).encode()).hexdigest()
            path.write_text(SourceInputStore.encode(envelope))
    report_path = root / "docs/reports/2026-10-06-gen_020-operational-report.json"
    report = json.loads(report_path.read_bytes())
    report["profile_sha256"] = digest
    request = publish_bound_report(report_path, report, old_request["generation_commit"])
    return config_path, request, report_path


def publish_bound_report(path, report, commit):
    raw, markdown = pair(report)
    path.write_bytes(raw)
    path.with_suffix(".md").write_bytes(markdown)
    return contract.build_repair_requests(raw, markdown, expected_generation="gen_020",
        expected_commit=commit, expected_session=report["session"], now=NOW)["requests"][0]


@pytest.mark.parametrize("relative", [False, True])
def test_export_replays_structured_filing_exclusion_with_exact_frozen_profile(evidence, relative):
    root, _, _, _, output = evidence
    config, request, _ = bind_profile(evidence, declared_filing=True, relative=relative)
    original = config.read_bytes()
    result = contract.export_repair_evidence(root, request, output, now=NOW)
    relative_path = "data/generations/gen_020/generation-config.json"
    exported = output / relative_path
    assert exported.read_bytes() == original
    assert exported.stat().st_mode & 0o777 == 0o400
    entry, = [item for item in result["files"] if item["path"] == relative_path]
    assert entry["sha256"] == hashlib.sha256(original).hexdigest()
    assert config.read_bytes() == original


@pytest.mark.parametrize("damage", [
    "wrong_path", "missing_file", "symlink", "missing_sha", "wrong_sha",
    "wrong_report_sha", "missing_report_sha", "profile_without_config",
    "metadata_sha_without_config", "invalid_config", "secret_config",
])
def test_export_rejects_unbound_or_unsafe_frozen_profile(evidence, damage):
    root, _, _, manifest, output = evidence
    config, request, report_path = bind_profile(evidence)
    metadata = json.loads(manifest.read_bytes())
    row = metadata["generations"][0]
    report = json.loads(report_path.read_bytes())
    if damage == "wrong_path":
        alternate = root / "alternate-generation-config.json"
        alternate.write_bytes(config.read_bytes())
        row["config_file"] = str(alternate)
    elif damage == "missing_file":
        config.unlink()
    elif damage == "symlink":
        saved = config.with_name("saved-config.json")
        config.replace(saved)
        config.symlink_to(saved)
    elif damage == "missing_sha":
        del row["config_sha256"]
    elif damage == "wrong_sha":
        row["config_sha256"] = "b" * 64
    elif damage == "wrong_report_sha":
        report["profile_sha256"] = "b" * 64
    elif damage == "missing_report_sha":
        del report["profile_sha256"]
    elif damage in {"profile_without_config", "metadata_sha_without_config"}:
        del row["config_file"]
        if damage == "metadata_sha_without_config":
            del report["profile_sha256"]
        else:
            del row["config_sha256"]
    else:
        config.write_bytes(b'[]' if damage == "invalid_config" else b'{"api_key":"simulated-secret"}')
        row["config_sha256"] = report["profile_sha256"] = hashlib.sha256(config.read_bytes()).hexdigest()
    manifest.write_text(json.dumps(metadata))
    request = publish_bound_report(report_path, report, request["generation_commit"])
    with pytest.raises((ValueError, OSError)):
        contract.export_repair_evidence(root, request, output, now=NOW)
    assert not output.exists()
    assert not list(output.parent.glob(".source-repair-*"))


def test_export_rechecks_frozen_config_before_publication(evidence, monkeypatch):
    root, _, _, _, output = evidence
    config, request, _ = bind_profile(evidence)
    original_verify = contract._Capture.verify

    def changed(capture):
        replacement = config.with_name("replacement-config.json")
        replacement.write_bytes(config.read_bytes())
        replacement.replace(config)
        return original_verify(capture)

    monkeypatch.setattr(contract._Capture, "verify", changed)
    with pytest.raises(contract.EvidenceChangedDuringCapture):
        contract.export_repair_evidence(root, request, output, now=NOW)
    assert not output.exists()
