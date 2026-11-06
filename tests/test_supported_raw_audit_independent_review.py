"""Independent path-manifest change reproduction; no external source calls."""
import hashlib

import pytest

from test_filing_supported_audit import audit, capture
from tradingagents.strategies.data_sources import filing_supported_policy


@pytest.mark.parametrize('replacement', ['directory', 'symlink'])
def test_capture_ancestor_replacement_during_verification_is_rejected(tmp_path, monkeypatch, replacement):
    data, paths = capture(tmp_path, monkeypatch)
    original = filing_supported_policy.build_local_source_proof
    changed = []

    def replace_ancestor(evidence, **kwargs):
        result = original(evidence, **kwargs)
        if not changed and evidence['accession'] == sorted(paths)[-1]:
            changed.append(evidence['accession'])
            directory = tmp_path / 'capture'
            retained = tmp_path / 'renamed-capture'
            directory.rename(retained)
            if replacement == 'symlink':
                directory.symlink_to(retained, target_is_directory=True)
            else:
                directory.mkdir()
                for path in paths.values():
                    path.write_bytes((retained / path.name).read_bytes())
                    path.chmod(0o400)
                # The opened mapping remains the exact original, but its
                # declared capture path now names different bytes/inode.
                paths[evidence['accession']].chmod(0o600)
                paths[evidence['accession']].write_bytes(b'wrong raw')
                paths[evidence['accession']].chmod(0o400)
                assert hashlib.sha256(paths[evidence['accession']].read_bytes()).hexdigest() != kwargs['original']['sha256']
        return result

    monkeypatch.setattr(filing_supported_policy, 'build_local_source_proof', replace_ancestor)
    with pytest.raises(ValueError, match='invalid_supported_raw_capture'):
        audit(data, paths)
    assert changed
