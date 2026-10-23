from copy import deepcopy
import importlib.util
import json
from pathlib import Path

import pytest

from test_form4_acquisition import acquire

SPEC = importlib.util.spec_from_file_location('qualify_sec_form4', Path(__file__).resolve().parents[1] / 'scripts' / 'qualify_sec_form4.py')


def runner():
    module = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(module)
    return module


def test_complete_acquisition_to_actual_consumer_request_and_zero_call_replay(monkeypatch):
    module = runner()
    source, _ = acquire(monkeypatch)
    value = module.build_receipt(source)
    assert value['model_calls'] == 0 and value['trading_authority'] is False
    assert value['production_changed'] is False
    assert len(value['requests']) == 1
    assert len(json.loads(value['requests'][0]['request']['user'])['documents'][0]['transactions']) == 11
    import requests
    monkeypatch.setattr(requests.sessions.Session, 'request', lambda *a, **k: pytest.fail('network during replay'))
    assert module.replay_receipt(value) == value


@pytest.mark.parametrize('damage', ['request', 'raw', 'context', 'code', 'receipt'])
def test_replay_recomputes_every_layer_and_detects_tampering(monkeypatch, damage):
    module = runner()
    source, _ = acquire(monkeypatch)
    value = module.build_receipt(source)
    if damage == 'request': value['requests'][0]['request']['user'] += 'changed'
    elif damage == 'raw': value['acquisition']['window']['documents'][0]['original_base64'] = 'e30='
    elif damage == 'context': value['requests'][0]['context']['selected_transaction_ids'].clear()
    elif damage == 'code': value['source_code_sha256'][next(iter(value['source_code_sha256']))] = '0'*64
    else: value['qualification_receipt_sha256'] = '0'*64
    with pytest.raises(ValueError, match='invalid_form4'):
        module.replay_receipt(value)


def test_capture_io_enforces_byte_bound_duplicate_fields_and_private_creation(tmp_path, monkeypatch):
    import hashlib
    import stat
    module = runner()
    target = tmp_path/'receipt.json'
    module.write_receipt(target, {'public': 'receipt'})
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError): module.write_receipt(target, {})
    assert module.read_capture(target, hashlib.sha256(target.read_bytes()).hexdigest()) == {'public': 'receipt'}
    repeated = tmp_path/'duplicate.json'; repeated.write_bytes(b'{"same":1,"same":2}')
    with pytest.raises(ValueError, match='invalid_form4_duplicate'):
        module.read_capture(repeated, hashlib.sha256(repeated.read_bytes()).hexdigest())
    monkeypatch.setattr(module, 'MAX_RECEIPT_BYTES', 8)
    with pytest.raises(ValueError, match='invalid_form4_capture'):
        module.read_capture(target, hashlib.sha256(target.read_bytes()).hexdigest())
