"""Scalar codec boundaries for source-inputs-v2 plain JSON + Parquet codec."""
import math

import numpy as np
import pytest

from tradingagents.strategies.orchestration import source_inputs as module


@pytest.mark.parametrize('value,canonical', [
    (None, 'null'), (False, 'false'), (True, 'true'), (0, '0'),
    (2 ** 100, str(2 ** 100)), ('filing', '"filing"'), (1.25, '1.25'),
    (float('nan'), '{"$t":"float","v":"nan"}'),
    (float('inf'), '{"$t":"float","v":"inf"}'),
    (float('-inf'), '{"$t":"float","v":"-inf"}'),
])
def test_exact_scalar_encode_and_decode_roundtrip(value, canonical):
    encoded = module.SourceInputStore.encode(value)
    assert encoded == canonical
    restored = module.SourceInputStore.decode(encoded)
    if isinstance(value, float) and math.isnan(value):
        assert math.isnan(restored)
    else:
        assert restored == value


@pytest.mark.parametrize('value', [None, True, 1, 1.25, 'filing', float('nan')])
def test_scalar_nesting_depth_is_bounded(value):
    with pytest.raises(module.SourceInputError, match='nesting depth'):
        module._to_plain(value, depth=module.MAX_DEPTH + 1)
    with pytest.raises(module.SourceInputError, match='nesting depth'):
        module._from_plain(value, depth=module.MAX_DEPTH + 1)


@pytest.mark.parametrize('value', [np.int64(3), np.uint64(4), np.float32(1.25),
                                  np.float64(1.25), np.bool_(True), np.str_('filing')])
def test_numpy_scalars_convert_canonically(value):
    canonical = module.SourceInputStore.encode(value.item())
    assert module.SourceInputStore.encode(value) == canonical


@pytest.mark.parametrize('base,value', [(int, 1), (float, 1.25), (str, 'filing')])
def test_builtin_subclasses_remain_unsupported(base, value):
    subclass = type('ScalarSubclass', (base,), {})
    with pytest.raises(module.SourceInputError, match='unsupported source value type'):
        module.SourceInputStore.encode(subclass(value))


@pytest.mark.parametrize('encoded', ['NaN', 'Infinity', '-Infinity',
                                     '{"$t":"float","v":"NaN"}',
                                     '{"$t":"float","v":"inf","extra":0}',
                                     '{"$t":"float","v":"inf","v":"nan"}'])
def test_scalar_decode_rejects_noncanonical_numbers_and_tags(encoded):
    with pytest.raises(module.SourceInputError):
        module.SourceInputStore.decode(encoded)


def test_scalar_list_keeps_utf8_byte_limits():
    payload = ['é', 1.25, float('nan')]
    encoded = module.SourceInputStore.encode(payload)
    assert module.SourceInputStore.encode(payload, max_bytes=len(encoded.encode())) == encoded
    with pytest.raises(module.SourceInputError, match='byte limit'):
        module.SourceInputStore.encode(payload, max_bytes=len(encoded.encode()) - 1)
    restored = module.SourceInputStore.decode(encoded)
    assert restored[:2] == payload[:2]
    assert math.isnan(restored[2])
