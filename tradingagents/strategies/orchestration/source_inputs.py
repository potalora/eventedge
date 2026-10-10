"""Bounded JSON provider cache and immutable accepted daily source observations.

Operational cache files can be discarded; accepted session bundles cannot be
replaced. Neither store writes accounting, manifests, or generation identity.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Any

import numpy as np
import pandas as pd

CONTRACT_VERSION = 'source-inputs-v1'
MAX_BYTES = 16 * 1024 * 1024
MAX_DEPTH = 48
MAX_NODES = 500_000
FULL_FILING_MAX_BYTES = 512 * 1024 * 1024
FULL_FILING_MAX_NODES = 8_000_000


class SourceInputError(ValueError):
    """A source document cannot safely authorize observation reuse."""


def _capacity(max_bytes=None, max_nodes=None) -> tuple[int, int]:
    byte_limit = MAX_BYTES if max_bytes is None else max_bytes
    node_limit = MAX_NODES if max_nodes is None else max_nodes
    if (type(byte_limit) is not int or not 1 <= byte_limit <= FULL_FILING_MAX_BYTES
            or type(node_limit) is not int or not 1 <= node_limit <= FULL_FILING_MAX_NODES):
        raise SourceInputError('invalid source capacity')
    return byte_limit, node_limit


def source_codec_limits(config: Mapping, *, source: str | None = None) -> dict[str, int]:
    """Full-filing corpora have explicit finite capacity; other stores keep defaults."""
    if (config.get('autoresearch', {}).get('filing_evidence_policy') == 'complete_submission_v1'
            and source in (None, 'edgar')):
        return {'max_bytes': FULL_FILING_MAX_BYTES, 'max_nodes': FULL_FILING_MAX_NODES}
    return {}


def _check_deadline(deadline: float | None) -> None:
    if deadline is not None:
        if type(deadline) not in (int, float) or not math.isfinite(deadline):
            raise SourceInputError('invalid source acquisition deadline')
        if time.monotonic() >= deadline:
            raise SourceInputError('source acquisition deadline exhausted')


def _dtype(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r'(object|bool|boolean|string|str|(?:u?int|float|Int|UInt|Float)(?:8|16|32|64)|datetime64\[(?:ns|us|ms|s)(?:, [A-Za-z0-9_+./:-]+)?\]|timedelta64\[(?:ns|us|ms|s)\])', value
    ):
        raise SourceInputError('unsupported pandas dtype')
    return value


def _transform(value: Any, *, decoding: bool, depth: int = 0, budget: list[int] | None = None) -> Any:
    budget = budget if budget is not None else [MAX_NODES]
    budget[0] -= 1
    if depth > MAX_DEPTH or budget[0] < 0:
        raise SourceInputError('source document exceeds structural bounds')
    def walk(item: Any) -> Any:
        return _transform(item, decoding=decoding, depth=depth + 1, budget=budget)
    if decoding:
        if value is None or type(value) in (str, bool, int):
            return value
        if type(value) is float:
            if not math.isfinite(value):
                raise SourceInputError('noncanonical JSON number')
            return value
        if isinstance(value, list):
            return [walk(item) for item in value]
        if not isinstance(value, dict) or '$type' not in value:
            raise SourceInputError('invalid typed JSON')
        tag = value['$type']
        fields = {
            'mapping': {'items'}, 'tuple': {'items'}, 'float': {'value'},
            'decimal': {'value'}, 'date': {'value'}, 'datetime': {'value'},
            'timestamp': {'value'}, 'missing': {'value'},
            'index': {'items', 'dtype', 'name'}, 'datetime_index': {'items', 'dtype', 'name', 'freq'},
            'range_index': {'start', 'stop', 'step', 'name'}, 'multi_index': {'items', 'names'},
            'series': {'items', 'dtype', 'index', 'name', 'attrs'},
            'frame': {'series', 'index', 'columns', 'attrs'},
        }
        if tag not in fields or set(value) != fields[tag] | {'$type'}:
            raise SourceInputError('invalid source type or fields')
        if tag == 'mapping':
            result = {}
            for pair in value['items']:
                if not isinstance(pair, list) or len(pair) != 2:
                    raise SourceInputError('invalid mapping entry')
                key, item = walk(pair[0]), walk(pair[1])
                if key in result:
                    raise SourceInputError('duplicate mapping key')
                result[key] = item
            return result
        if tag == 'tuple':
            return tuple(walk(item) for item in value['items'])
        if tag == 'float':
            if value['value'] not in ('nan', 'inf', '-inf'):
                raise SourceInputError('invalid special float')
            return float(value['value'])
        if tag == 'decimal':
            return Decimal(value['value'])
        if tag == 'date':
            return date.fromisoformat(value['value'])
        if tag == 'datetime':
            return datetime.fromisoformat(value['value'])
        if tag == 'timestamp':
            return pd.Timestamp(value['value'])
        if tag == 'missing':
            if value['value'] not in ('NA', 'NaT'):
                raise SourceInputError('invalid missing value')
            return pd.NA if value['value'] == 'NA' else pd.NaT
        if tag == 'range_index':
            return pd.RangeIndex(value['start'], value['stop'], value['step'], name=walk(value['name']))
        if tag == 'multi_index':
            return pd.MultiIndex.from_tuples([walk(item) for item in value['items']], names=walk(value['names']))
        if tag in ('index', 'datetime_index'):
            items, name = [walk(item) for item in value['items']], walk(value['name'])
            dtype = _dtype(value['dtype'])
            if tag == 'datetime_index':
                return pd.DatetimeIndex(items, dtype=dtype, name=name, freq=value['freq'])
            return pd.Index(items, dtype=dtype, name=name, tupleize_cols=False)
        if tag == 'series':
            result = pd.Series([walk(item) for item in value['items']], index=walk(value['index']),
                               dtype=_dtype(value['dtype']), name=walk(value['name']))
            result.attrs = walk(value['attrs'])
            return result
        series, index, columns = walk(value['series']), walk(value['index']), walk(value['columns'])
        if len(series) != len(columns) or any(len(item) != len(index) for item in series):
            raise SourceInputError('invalid dataframe dimensions')
        result = pd.concat(series, axis=1) if series else pd.DataFrame(index=index)
        result.index, result.columns = index, columns
        result.attrs = walk(value['attrs'])
        return result

    if value is pd.NA or value is pd.NaT:
        return {'$type': 'missing', 'value': 'NA' if value is pd.NA else 'NaT'}
    if isinstance(value, pd.DataFrame):
        return {'$type': 'frame', 'series': walk([value.iloc[:, i] for i in range(len(value.columns))]),
                'index': walk(value.index), 'columns': walk(value.columns), 'attrs': walk(value.attrs)}
    if isinstance(value, pd.Series):
        return {'$type': 'series', 'items': [walk(item) for item in value.tolist()],
                'dtype': _dtype(str(value.dtype)), 'index': walk(value.index),
                'name': walk(value.name), 'attrs': walk(value.attrs)}
    if isinstance(value, pd.RangeIndex):
        return {'$type': 'range_index', 'start': value.start, 'stop': value.stop, 'step': value.step, 'name': walk(value.name)}
    if isinstance(value, pd.MultiIndex):
        return {'$type': 'multi_index', 'items': [walk(item) for item in value.tolist()], 'names': walk(list(value.names))}
    if isinstance(value, pd.Index):
        result = {'$type': 'datetime_index' if isinstance(value, pd.DatetimeIndex) else 'index',
                  'items': [walk(item) for item in value.tolist()], 'dtype': _dtype(str(value.dtype)), 'name': walk(value.name)}
        if isinstance(value, pd.DatetimeIndex):
            result['freq'] = value.freqstr
        return result
    if isinstance(value, pd.Timestamp):
        return {'$type': 'timestamp', 'value': value.isoformat()}
    if isinstance(value, datetime):
        return {'$type': 'datetime', 'value': value.isoformat()}
    if isinstance(value, date):
        return {'$type': 'date', 'value': value.isoformat()}
    if isinstance(value, Decimal):
        return {'$type': 'decimal', 'value': str(value)}
    if isinstance(value, Mapping):
        return {'$type': 'mapping', 'items': [[walk(key), walk(item)] for key, item in value.items()]}
    if isinstance(value, tuple):
        return {'$type': 'tuple', 'items': [walk(item) for item in value]}
    if isinstance(value, list):
        return [walk(item) for item in value]
    if isinstance(value, np.generic):
        return walk(value.item())
    if type(value) is float:
        return value if math.isfinite(value) else {'$type': 'float', 'value': str(value)}
    if value is None or type(value) in (str, int, bool):
        return value
    raise SourceInputError('unsupported source value type')


def configuration_fingerprint(config: Mapping[str, Any]) -> str:
    """Hash config canonically without persisting its credentials or values."""
    def ordered(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {key: ordered(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
        if isinstance(value, (tuple, list)):
            return [ordered(item) for item in value]
        if isinstance(value, Path):
            return str(value)
        return value
    return hashlib.sha256(SourceInputStore.encode(ordered(config)).encode()).hexdigest()


def source_configuration_fingerprint(config: Mapping[str, Any], *, exclude_horizon: bool = False) -> str:
    """Bind effective credential fallbacks without saving credentials in documents."""
    ar = dict(config.get('autoresearch', {}))
    credentials = {
        'fmp_api_key': 'FMP_API_KEY', 'fred_api_key': 'FRED_API_KEY',
        'finnhub_api_key': 'FINNHUB_API_KEY', 'regulations_api_key': 'REGULATIONS_API_KEY',
        'courtlistener_token': 'COURTLISTENER_TOKEN', 'noaa_cdo_token': 'NOAA_CDO_TOKEN',
        'usda_nass_api_key': 'USDA_NASS_API_KEY',
    }
    for key, environment in credentials.items():
        ar[key] = ar.get(key) or os.environ.get(environment, '')
    if ar.get('equity_universe_policy'):
        # The asset-master adapter consumes environment credentials exclusively.
        ar['equity_universe_credentials'] = {
            'key': os.environ.get('ALPACA_API_KEY', '').strip(),
            'secret': os.environ.get('ALPACA_SECRET_KEY', '').strip(),
        }
    omitted = {'state_dir', 'source_cache_dir', 'source_cache_ttl_s'}
    if exclude_horizon:
        omitted.add('horizon')
    effective = dict(config)
    # Post-staging shadow classification does not govern source observations.
    effective.pop('decision_shadow', None)
    effective['autoresearch'] = {key: value for key, value in ar.items() if key not in omitted}
    return configuration_fingerprint(effective)


def registered_source_fingerprint(configuration: str, source: Any) -> str:
    settings = {'configuration': configuration}
    if source is not None:
        settings['adapter'] = type(source).__module__ + '.' + type(source).__qualname__
        settings['credentials'] = {
            key: getattr(source, key) for key in ('_api_key', '_token', '_fmp_api_key', '_user_agent')
            if isinstance(getattr(source, key, None), str)
        }
    return configuration_fingerprint(settings)


def cache_identity(source: str, start: str, session: str, configuration: str) -> dict[str, str]:
    date.fromisoformat(start)
    date.fromisoformat(session)
    return {'source': source, 'window_start': start, 'session': session,
            'configuration': configuration, 'contract': CONTRACT_VERSION}


def successful_source(payload: Any) -> bool:
    if not isinstance(payload, Mapping) or payload.get('error') not in (None, ''):
        return False
    coverage = payload.get('_coverage')
    if '_coverage' in payload and (
        not isinstance(coverage, Mapping)
        or coverage.get('status') not in ('success', 'success_empty', 'complete')
    ):
        return False
    return True


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise SourceInputError('acquisition time must be timezone-aware')
    return value.astimezone(timezone.utc)


class SourceInputStore:
    def __init__(self, cache_dir: str | Path, *, accepted_dir: str | Path | None = None,
                 ttl_s: float = 300, max_bytes: int | None = None, max_nodes: int | None = None):
        if not math.isfinite(float(ttl_s)) or not 0 < float(ttl_s) <= 300:
            raise SourceInputError('cache ttl must be between zero and 300 seconds')
        self.cache_dir = Path(cache_dir)
        self.accepted_dir = Path(accepted_dir) if accepted_dir is not None else None
        self.ttl_s = float(ttl_s)
        _capacity(max_bytes, max_nodes)
        self._codec_limits = {'max_bytes': max_bytes, 'max_nodes': max_nodes}

    @property
    def codec_limits(self) -> dict[str, int | None]:
        return dict(self._codec_limits)

    @staticmethod
    def encode(payload: Any, *, max_bytes: int | None = None, max_nodes: int | None = None) -> str:
        byte_limit, node_limit = _capacity(max_bytes, max_nodes)
        try:
            encoded = json.dumps(_transform(payload, decoding=False, budget=[node_limit]),
                                 allow_nan=False, separators=(',', ':'), sort_keys=True)
            if len(encoded.encode()) > byte_limit:
                raise SourceInputError('source document exceeds byte limit')
            return encoded
        except SourceInputError:
            raise
        except (ValueError, TypeError, OverflowError, RecursionError) as error:
            raise SourceInputError('invalid source document') from error

    @staticmethod
    def decode(encoded: str, *, max_bytes: int | None = None, max_nodes: int | None = None) -> Any:
        byte_limit, node_limit = _capacity(max_bytes, max_nodes)
        try:
            if not isinstance(encoded, str) or len(encoded.encode()) > byte_limit:
                raise SourceInputError('source document exceeds byte limit')
            def pairs(entries):
                result = {}
                for key, value in entries:
                    if key in result:
                        raise SourceInputError('duplicate JSON field')
                    result[key] = value
                return result
            return _transform(json.loads(encoded, object_pairs_hook=pairs), decoding=True, budget=[node_limit])
        except SourceInputError:
            raise
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError, AttributeError) as error:
            raise SourceInputError('invalid source document') from error

    def _path(self, identity: Mapping, *, frozen: bool) -> Path:
        if frozen:
            if self.accepted_dir is None:
                raise SourceInputError('accepted source directory not configured')
            slot = {'generation': identity['generation'], 'session': identity['session']}
            root = self.accepted_dir
        else:
            slot, root = identity, self.cache_dir
        return root / (configuration_fingerprint(slot) + '.json')

    def _read(self, path: Path, identity: Mapping) -> dict:
        byte_limit, _ = _capacity(**self._codec_limits)
        if path.stat().st_size > byte_limit:
            raise SourceInputError('source document exceeds byte limit')
        document = self.decode(path.read_text(), **self._codec_limits)
        if not isinstance(document, dict) or set(document) != {'version', 'identity', 'acquired_at', 'payload', 'digest'}:
            raise SourceInputError('invalid source envelope')
        if document['version'] != CONTRACT_VERSION or document['identity'] != dict(identity):
            raise SourceInputError('source identity mismatch')
        if document['digest'] != hashlib.sha256(self.encode(document['payload'], **self._codec_limits).encode()).hexdigest():
            raise SourceInputError('source payload digest mismatch')
        _utc(document['acquired_at'])
        return document

    @staticmethod
    def _reject_owned(path: Path, inode: tuple[int, int] | None) -> None:
        """Retain our rejected publication without evicting a concurrent winner."""
        if inode is None:
            return
        try:
            existing = path.lstat()
        except FileNotFoundError:
            return
        if (existing.st_dev, existing.st_ino) != inode:
            return
        rejected = path.parent / 'rejected'
        rejected.mkdir(exist_ok=True)
        fd, name = tempfile.mkstemp(dir=rejected, prefix=path.stem + '-', suffix='.json')
        os.close(fd)
        # Frozen names are exclusive-only and never overwritten by this store.
        # Matching the inode restricts retirement to this invocation's file.
        os.replace(path, name)

    def _write(self, path: Path, identity: Mapping, payload: Any, acquired_at: datetime, *,
               exclusive: bool, deadline: float | None = None) -> tuple[int, int] | None:
        _check_deadline(deadline)
        document = {'version': CONTRACT_VERSION, 'identity': dict(identity), 'acquired_at': _utc(acquired_at),
                    'payload': payload, 'digest': hashlib.sha256(self.encode(payload, **self._codec_limits).encode()).hexdigest()}
        encoded = self.encode(document, **self._codec_limits)
        _check_deadline(deadline)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=path.parent, prefix='.source-')
        published = None
        try:
            with os.fdopen(fd, 'w') as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            _check_deadline(deadline)
            if exclusive:
                info = Path(name).stat()
                os.link(name, path)
                published = (info.st_dev, info.st_ino)
            else:
                os.replace(name, path)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            _check_deadline(deadline)
            return published
        except (OSError, SourceInputError):
            self._reject_owned(path, published)
            raise
        finally:
            Path(name).unlink(missing_ok=True)

    def load_cached(self, identity: Mapping, *, now: datetime | None = None, cutoff: datetime | None = None) -> Any | None:
        now = _utc(now or datetime.now(timezone.utc))
        cutoff = _utc(cutoff or now)
        try:
            document = self._read(self._path(identity, frozen=False), identity)
            acquired = document['acquired_at']
            if acquired > min(now, cutoff) or (now - acquired).total_seconds() > self.ttl_s:
                return None
            return document['payload'] if successful_source(document['payload']) else None
        except (OSError, SourceInputError):
            return None

    def save_cached(self, identity: Mapping, payload: Any, *, acquired_at: datetime | None = None) -> bool:
        if not successful_source(payload):
            return False
        self._write(self._path(identity, frozen=False), identity, payload,
                    acquired_at or datetime.now(timezone.utc), exclusive=False)
        return True

    def load_frozen(self, identity: Mapping) -> Any | None:
        try:
            document = self._read(self._path(identity, frozen=True), identity)
            if document['acquired_at'] > datetime.now(timezone.utc):
                raise SourceInputError('future acquisition time')
            return document['payload']
        except FileNotFoundError:
            return None
        except (OSError, SourceInputError) as error:
            raise SourceInputError(f'frozen source bundle rejected: {error}') from error

    def freeze(self, identity: Mapping, payload: Any, *, acquired_at: datetime | None = None,
               deadline: float | None = None) -> Any:
        _check_deadline(deadline)
        existing = self.load_frozen(identity)
        _check_deadline(deadline)
        if existing is not None:
            return existing
        path = self._path(identity, frozen=True)
        published = None
        try:
            published = self._write(path, identity, payload,
                        acquired_at or datetime.now(timezone.utc), exclusive=True, deadline=deadline)
        except FileExistsError:
            pass  # A concurrent first writer owns the accepted observations.
        try:
            result = self.load_frozen(identity)
            _check_deadline(deadline)
        except (OSError, SourceInputError):
            self._reject_owned(path, published)
            raise
        return result


def daily_source_store(owner: Any, session: str) -> tuple[SourceInputStore, dict[str, str]]:
    """Locate accepted inputs using the orchestrator's bound generation identity."""
    config = owner._base_config
    ar = config.get('autoresearch', {})
    generation = owner._metric_epoch_context
    identity = {
        'generation': generation.generation_id, 'session': session,
        'commit': generation.generation_commit,
        'configuration': source_configuration_fingerprint(config),
    }
    state_dir = Path(ar.get('state_dir', 'data/state'))
    cache_dir = ar.get('source_cache_dir') or os.environ.get('EVENTEDGE_SOURCE_CACHE_DIR') or state_dir / 'source_cache'
    return SourceInputStore(cache_dir, accepted_dir=state_dir / 'source_inputs',
                            ttl_s=ar.get('source_cache_ttl_s', 300),
                            **source_codec_limits(config)), identity


def daily_volatility_store(owner: Any, session: str) -> tuple[SourceInputStore, dict[str, str]]:
    """Keep accepted staging history separate from the earlier screening inputs."""
    shared, identity = daily_source_store(owner, session)
    identity = {**identity, 'purpose': 'staging-volatility-v1'}
    return SourceInputStore(
        shared.cache_dir,
        accepted_dir=shared.accepted_dir / 'staging_volatility',
        ttl_s=shared.ttl_s,
    ), identity
