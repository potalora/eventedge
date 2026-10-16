"""Scalar codec cost and strict boundaries, without wall-clock assertions."""
import builtins
import math

import numpy as np
import pytest

from tradingagents.strategies.orchestration import source_inputs as module


@pytest.mark.parametrize('value,canonical', [
    (None, 'null'), (False, 'false'), (True, 'true'), (0, '0'),
    (2 ** 100, str(2 ** 100)), ('filing', '"filing"'), (1.25, '1.25'),
    (float('nan'), '{"$type":"float","value":"nan"}'),
    (float('inf'), '{"$type":"float","value":"inf"}'),
    (float('-inf'), '{"$type":"float","value":"-inf"}'),
])
def test_exact_scalar_encode_avoids_expensive_type_dispatch(monkeypatch, value, canonical):
    # Millions of scalar leaves must not traverse the pandas/NumPy type chain.
    def checked_isinstance(item, classes):
        assert item is not value, 'exact scalar entered expensive type dispatch'
        return builtins.isinstance(item, classes)
    monkeypatch.setattr(module, 'isinstance', checked_isinstance, raising=False)
    assert module.SourceInputStore.encode(value) == canonical


@pytest.mark.parametrize('value', [None, True, 1, 1.25, 'filing', float('nan')])
@pytest.mark.parametrize('decoding', [False, True])
def test_scalar_fast_path_still_accounts_for_node_and_depth(value, decoding):
    with pytest.raises(module.SourceInputError, match='structural bounds'):
        module._transform(value, decoding=decoding, budget=[0])
    with pytest.raises(module.SourceInputError, match='structural bounds'):
        module._transform(value, decoding=decoding, depth=module.MAX_DEPTH + 1)


@pytest.mark.parametrize('value', [np.int64(3), np.uint64(4), np.float32(1.25),
                                  np.float64(1.25), np.bool_(True), np.str_('filing')])
def test_numpy_scalars_keep_conversion_and_extra_node_accounting(value):
    canonical = module.SourceInputStore.encode(value.item())
    assert module.SourceInputStore.encode(value) == canonical
    with pytest.raises(module.SourceInputError, match='structural bounds'):
        module.SourceInputStore.encode(value, max_nodes=1)
    assert module.SourceInputStore.encode(value, max_nodes=2) == canonical


@pytest.mark.parametrize('base,value', [(int, 1), (float, 1.25), (str, 'filing')])
def test_builtin_subclasses_remain_unsupported(base, value):
    subclass = type('ScalarSubclass', (base,), {})
    with pytest.raises(module.SourceInputError, match='unsupported source value type'):
        module.SourceInputStore.encode(subclass(value))


@pytest.mark.parametrize('encoded', ['NaN', 'Infinity', '-Infinity',
                                     '{"$type":"float","value":"NaN"}',
                                     '{"$type":"float","value":"inf","extra":0}',
                                     '{"$type":"float","value":"inf","value":"nan"}'])
def test_scalar_decode_rejects_noncanonical_numbers_and_tags(encoded):
    with pytest.raises(module.SourceInputError):
        module.SourceInputStore.decode(encoded)


def test_scalar_list_keeps_exact_node_and_utf8_byte_limits():
    payload = ['é', 1.25, float('nan')]
    encoded = module.SourceInputStore.encode(payload, max_nodes=4)
    with pytest.raises(module.SourceInputError, match='structural bounds'):
        module.SourceInputStore.encode(payload, max_nodes=3)
    assert module.SourceInputStore.encode(payload, max_bytes=len(encoded.encode())) == encoded
    with pytest.raises(module.SourceInputError, match='byte limit'):
        module.SourceInputStore.encode(payload, max_bytes=len(encoded.encode()) - 1)
    restored = module.SourceInputStore.decode(encoded, max_nodes=4)
    assert restored[:2] == payload[:2]
    assert math.isnan(restored[2])
