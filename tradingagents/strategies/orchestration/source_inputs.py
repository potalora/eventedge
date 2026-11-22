"""Parquet + plain JSON provider cache and immutable accepted daily source bundles.

DataFrames and Series are stored in Apache Parquet (with gzipped JSON-records
fallback when pyarrow is unavailable or a frame contains non-Arrow Python
objects), eliminating structural node-count caps and custom pandas-in-JSON AST
walking. Metadata envelopes are strict JSON written atomically with fsync.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from datetime import date, datetime, timezone
from decimal import Decimal
import gzip
import hashlib
import io
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

from tradingagents.strategies.orchestration._io import atomic_write_text, load_json_strict

CONTRACT_VERSION = "source-inputs-v2"
MAX_BYTES = 256 * 1024 * 1024
MAX_DEPTH = 64
PUBLIC_CONFIGURATION_KEY = "_source_public_configuration"
PUBLIC_CONFIGURATION_VERSION = "source-public-configuration-v2"
_SOURCE_CREDENTIALS = frozenset(
    {
        "fmp_api_key",
        "fred_api_key",
        "finnhub_api_key",
        "noaa_cdo_token",
        "usda_nass_api_key",
        "equity_universe_credentials",
    }
)


def _has_pyarrow() -> bool:
    try:
        import pyarrow  # noqa: F401
        return True
    except ImportError:
        return False


class SourceInputError(ValueError):
    """A source document cannot safely authorize observation reuse."""


def source_codec_limits(config: Mapping, *, source: str | None = None) -> dict[str, int]:
    """Retained for API compatibility; Parquet storage removes node-count caps."""
    _ = (config, source)
    return {}


def _check_deadline(deadline: float | None) -> None:
    if deadline is not None:
        if type(deadline) not in (int, float) or not math.isfinite(deadline):
            raise SourceInputError("invalid source acquisition deadline")
        if time.monotonic() >= deadline:
            raise SourceInputError("source acquisition deadline exhausted")


def _encode_frame_bytes(frame: pd.DataFrame) -> tuple[str, bytes, dict[str, Any]]:
    meta: dict[str, Any] = {
        "attrs": _to_plain(frame.attrs, depth=0),
        "index_freq": (
            frame.index.freqstr if isinstance(frame.index, pd.DatetimeIndex) else None
        ),
        "index_dtype": (
            str(frame.index.dtype) if isinstance(frame.index, pd.DatetimeIndex) else None
        ),
        "empty": frame.empty,
        "columns": [str(c) for c in frame.columns],
        "index_name": frame.index.name,
    }
    if _has_pyarrow() and len(frame.columns) > 0:
        try:
            buf = io.BytesIO()
            # Attrs are already persisted deterministically in meta["attrs"];
            # clearing attrs on a shallow copy avoids dict-insertion-order drift
            # in pyarrow's PANDAS_ATTRS schema metadata across encode/decode roundtrips.
            clean = frame.copy(deep=False)
            clean.attrs = {}
            clean.to_parquet(buf, engine="pyarrow", index=True)
            return "parquet", buf.getvalue(), meta
        except Exception:
            pass
    # Fallback: gzipped JSON records preserving index + dtypes
    fallback_payload = {
        "columns": _to_plain(list(frame.columns), depth=0),
        "index": _to_plain(frame.index.tolist(), depth=0),
        "index_type": type(frame.index).__name__,
        "index_tz": str(frame.index.tz) if getattr(frame.index, "tz", None) is not None else None,
        "dtypes": {str(c): str(dt) for c, dt in frame.dtypes.items()},
        "data": {str(c): _to_plain(frame[c].tolist(), depth=0) for c in frame.columns},
    }
    raw = gzip.compress(
        json.dumps(fallback_payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    )
    return "gzip_json", raw, meta


def _decode_frame_bytes(fmt: str, raw: bytes, meta: Mapping[str, Any]) -> pd.DataFrame:
    if fmt == "parquet":
        frame = pd.read_parquet(io.BytesIO(raw), engine="pyarrow")
        if isinstance(frame.index, pd.DatetimeIndex):
            index = frame.index
            dtype_str = meta.get("index_dtype")
            if isinstance(dtype_str, str) and dtype_str.startswith("datetime64"):
                try:
                    index = index.astype(dtype_str)
                except Exception:
                    pass
            freq = meta.get("index_freq")
            if freq:
                try:
                    index = pd.DatetimeIndex(index, freq=freq)
                except Exception:
                    pass
            frame.index = index
    elif fmt == "gzip_json":
        obj = json.loads(gzip.decompress(raw).decode("utf-8"))
        cols = _from_plain(obj["columns"], depth=0)
        idx_vals = _from_plain(obj["index"], depth=0)
        if obj.get("index_type") == "DatetimeIndex":
            idx: pd.Index = pd.DatetimeIndex(idx_vals, freq=meta.get("index_freq"))
            if obj.get("index_tz") and idx.tz is None:
                idx = idx.tz_localize(obj["index_tz"])
        else:
            idx = pd.Index(idx_vals)
        idx.name = meta.get("index_name")
        data = {c: _from_plain(obj["data"][str(c)], depth=0) for c in cols}
        frame = pd.DataFrame(data, index=idx, columns=cols)
        for col_name, dtype_str in obj.get("dtypes", {}).items():
            if col_name in frame.columns:
                try:
                    frame[col_name] = frame[col_name].astype(dtype_str)
                except Exception:
                    pass
    else:
        raise SourceInputError(f"unsupported table format: {fmt!r}")
    frame.attrs = _from_plain(meta.get("attrs", {}), depth=0)
    return frame


def _to_plain(
    value: Any,
    *,
    depth: int = 0,
    table_dir: Path | None = None,
    table_counter: list[int] | None = None,
) -> Any:
    if depth > MAX_DEPTH:
        raise SourceInputError("source document exceeds nesting depth")
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float:
        return value if math.isfinite(value) else {"$t": "float", "v": str(value)}
    if value is pd.NA or value is pd.NaT:
        return {"$t": "missing", "v": "NA" if value is pd.NA else "NaT"}
    if isinstance(value, pd.DataFrame):
        fmt, raw, meta = _encode_frame_bytes(value)
        return _store_table("frame", fmt, raw, meta, table_dir, table_counter)
    if isinstance(value, pd.Series):
        frame = value.to_frame(name="__series__")
        fmt, raw, meta = _encode_frame_bytes(frame)
        meta["series_name"] = _to_plain(value.name, depth=depth + 1)
        meta["series_attrs"] = _to_plain(value.attrs, depth=depth + 1)
        return _store_table("series", fmt, raw, meta, table_dir, table_counter)
    if isinstance(value, pd.Timestamp):
        return {"$t": "timestamp", "v": value.isoformat()}
    if isinstance(value, datetime):
        return {"$t": "datetime", "v": value.isoformat()}
    if isinstance(value, date):
        return {"$t": "date", "v": value.isoformat()}
    if isinstance(value, Decimal):
        return {"$t": "decimal", "v": str(value)}
    if isinstance(value, np.generic):
        return _to_plain(value.item(), depth=depth + 1, table_dir=table_dir, table_counter=table_counter)
    if isinstance(value, tuple):
        return {
            "$t": "tuple",
            "v": [_to_plain(x, depth=depth + 1, table_dir=table_dir, table_counter=table_counter) for x in value],
        }
    if isinstance(value, list):
        return [_to_plain(x, depth=depth + 1, table_dir=table_dir, table_counter=table_counter) for x in value]
    if isinstance(value, Mapping):
        if "$t" in value or any(type(k) is not str for k in value):
            return {
                "$t": "mapping",
                "v": [
                    [
                        _to_plain(k, depth=depth + 1, table_dir=table_dir, table_counter=table_counter),
                        _to_plain(v, depth=depth + 1, table_dir=table_dir, table_counter=table_counter),
                    ]
                    for k, v in value.items()
                ],
            }
        return {
            k: _to_plain(v, depth=depth + 1, table_dir=table_dir, table_counter=table_counter)
            for k, v in value.items()
        }
    raise SourceInputError(f"unsupported source value type: {type(value).__name__}")


def _store_table(
    kind: str,
    fmt: str,
    raw: bytes,
    meta: dict[str, Any],
    table_dir: Path | None,
    table_counter: list[int] | None,
) -> dict[str, Any]:
    sha = hashlib.sha256(raw).hexdigest()
    if table_dir is not None and table_counter is not None:
        idx = table_counter[0]
        table_counter[0] += 1
        ext = "parquet" if fmt == "parquet" else "json.gz"
        filename = f"t{idx}_{sha[:12]}.{ext}"
        table_dir.mkdir(parents=True, exist_ok=True)
        target = table_dir / filename
        fd, tmp_name = tempfile.mkstemp(dir=table_dir, prefix=f".{filename}.")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, target)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
        return {"$t": kind, "fmt": fmt, "file": filename, "sha256": sha, "meta": meta}
    return {
        "$t": kind,
        "fmt": fmt,
        "b64": base64.b64encode(raw).decode("ascii"),
        "sha256": sha,
        "meta": meta,
    }


def _legacy_dtype(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"(object|bool|boolean|string|str|(?:u?int|float|Int|UInt|Float)(?:8|16|32|64)|datetime64\[(?:ns|us|ms|s)(?:, [A-Za-z0-9_+./:-]+)?\]|timedelta64\[(?:ns|us|ms|s)\])",
        value,
    ):
        raise SourceInputError("unsupported pandas dtype")
    return value


_LEGACY_TYPE_FIELDS: dict[str, set[str]] = {
    "mapping": {"items"},
    "tuple": {"items"},
    "float": {"value"},
    "decimal": {"value"},
    "date": {"value"},
    "datetime": {"value"},
    "timestamp": {"value"},
    "missing": {"value"},
    "index": {"items", "dtype", "name"},
    "datetime_index": {"items", "dtype", "name", "freq"},
    "range_index": {"start", "stop", "step", "name"},
    "multi_index": {"items", "names"},
    "series": {"items", "dtype", "index", "name", "attrs"},
    "frame": {"series", "index", "columns", "attrs"},
}


def _from_legacy_typed(value: dict[str, Any], *, depth: int, table_dir: Path | None) -> Any:
    tag = value["$type"]
    if tag not in _LEGACY_TYPE_FIELDS or set(value) != _LEGACY_TYPE_FIELDS[tag] | {"$type"}:
        raise SourceInputError("invalid source type or fields")

    def walk(item: Any) -> Any:
        return _from_plain(item, depth=depth + 1, table_dir=table_dir)

    if tag == "mapping":
        result = {}
        for pair in value["items"]:
            if not isinstance(pair, list) or len(pair) != 2:
                raise SourceInputError("invalid mapping entry")
            key, item = walk(pair[0]), walk(pair[1])
            if key in result:
                raise SourceInputError("duplicate mapping key")
            result[key] = item
        return result
    if tag == "tuple":
        return tuple(walk(item) for item in value["items"])
    if tag == "float":
        if value["value"] not in ("nan", "inf", "-inf"):
            raise SourceInputError("invalid special float")
        return float(value["value"])
    if tag == "decimal":
        return Decimal(value["value"])
    if tag == "date":
        return date.fromisoformat(value["value"])
    if tag == "datetime":
        return datetime.fromisoformat(value["value"])
    if tag == "timestamp":
        return pd.Timestamp(value["value"])
    if tag == "missing":
        if value["value"] not in ("NA", "NaT"):
            raise SourceInputError("invalid missing value")
        return pd.NA if value["value"] == "NA" else pd.NaT
    if tag == "range_index":
        return pd.RangeIndex(value["start"], value["stop"], value["step"], name=walk(value["name"]))
    if tag == "multi_index":
        return pd.MultiIndex.from_tuples([walk(item) for item in value["items"]], names=walk(value["names"]))
    if tag in ("index", "datetime_index"):
        items, name = [walk(item) for item in value["items"]], walk(value["name"])
        dtype = _legacy_dtype(value["dtype"])
        if tag == "datetime_index":
            return pd.DatetimeIndex(items, dtype=dtype, name=name, freq=value["freq"])
        return pd.Index(items, dtype=dtype, name=name, tupleize_cols=False)
    if tag == "series":
        result = pd.Series(
            [walk(item) for item in value["items"]],
            index=walk(value["index"]),
            dtype=_legacy_dtype(value["dtype"]),
            name=walk(value["name"]),
        )
        result.attrs = walk(value["attrs"])
        return result
    series, index, columns = walk(value["series"]), walk(value["index"]), walk(value["columns"])
    if len(series) != len(columns) or any(len(item) != len(index) for item in series):
        raise SourceInputError("invalid dataframe dimensions")
    result = pd.concat(series, axis=1) if series else pd.DataFrame(index=index)
    result.index, result.columns = index, columns
    result.attrs = walk(value["attrs"])
    return result


def _from_plain(value: Any, *, depth: int = 0, table_dir: Path | None = None) -> Any:
    if depth > MAX_DEPTH:
        raise SourceInputError("source document exceeds nesting depth")
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise SourceInputError("noncanonical JSON number")
        return value
    if isinstance(value, list):
        return [_from_plain(x, depth=depth + 1, table_dir=table_dir) for x in value]
    if isinstance(value, dict):
        legacy_tag = value.get("$type")
        if (
            isinstance(legacy_tag, str)
            and legacy_tag in _LEGACY_TYPE_FIELDS
            and set(value) == _LEGACY_TYPE_FIELDS[legacy_tag] | {"$type"}
        ):
            return _from_legacy_typed(value, depth=depth, table_dir=table_dir)
        if "$t" not in value:
            return {k: _from_plain(v, depth=depth + 1, table_dir=table_dir) for k, v in value.items()}
        tag = value["$t"]
        if tag == "float":
            if set(value.keys()) != {"$t", "v"} or value.get("v") not in ("nan", "inf", "-inf"):
                raise SourceInputError("invalid special float")
            return float(value["v"])
        if tag == "missing":
            if value.get("v") not in ("NA", "NaT"):
                raise SourceInputError("invalid missing value")
            return pd.NA if value["v"] == "NA" else pd.NaT
        if tag == "decimal":
            return Decimal(value["v"])
        if tag == "date":
            return date.fromisoformat(value["v"])
        if tag == "datetime":
            return datetime.fromisoformat(value["v"])
        if tag == "timestamp":
            return pd.Timestamp(value["v"])
        if tag == "tuple":
            return tuple(_from_plain(x, depth=depth + 1, table_dir=table_dir) for x in value["v"])
        if tag == "mapping":
            result = {}
            for pair in value["v"]:
                if not isinstance(pair, list) or len(pair) != 2:
                    raise SourceInputError("invalid mapping entry")
                k = _from_plain(pair[0], depth=depth + 1, table_dir=table_dir)
                if k in result:
                    raise SourceInputError("duplicate mapping key")
                result[k] = _from_plain(pair[1], depth=depth + 1, table_dir=table_dir)
            return result
        if tag in ("frame", "series"):
            fmt = value.get("fmt")
            meta = value.get("meta", {})
            expected_sha = value.get("sha256")
            if "file" in value:
                if table_dir is None:
                    raise SourceInputError("table sidecar directory missing")
                raw = (table_dir / str(value["file"])).read_bytes()
            elif "b64" in value:
                raw = base64.b64decode(value["b64"].encode("ascii"))
            else:
                raise SourceInputError("missing table data")
            if expected_sha and hashlib.sha256(raw).hexdigest() != expected_sha:
                raise SourceInputError("table checksum mismatch")
            frame = _decode_frame_bytes(fmt, raw, meta)
            if tag == "series":
                series = frame["__series__"]
                series.name = _from_plain(meta.get("series_name"), depth=depth + 1)
                series.attrs = _from_plain(meta.get("series_attrs", {}), depth=depth + 1)
                return series
            return frame
        raise SourceInputError(f"unknown source tag: {tag!r}")
    raise SourceInputError("invalid source document")


def configuration_fingerprint(config: Mapping[str, Any]) -> str:
    """Hash config canonically without persisting credentials."""
    def ordered(value: Any) -> Any:
        if hasattr(value, "to_dict") and callable(value.to_dict):
            return ordered(value.to_dict())
        if isinstance(value, Mapping):
            return {str(k): ordered(v) for k, v in sorted(value.items(), key=lambda p: str(p[0]))}
        if isinstance(value, (tuple, list)):
            return [ordered(v) for v in value]
        if isinstance(value, Path):
            return str(value)
        return value

    encoded = SourceInputStore.encode(ordered(config))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def source_configuration_fingerprint(config: Mapping[str, Any], *, exclude_horizon: bool = False) -> str:
    """Bind effective credential fallbacks without saving credentials in documents."""
    ar = dict(config.get("autoresearch", {}))
    credentials = {
        "fmp_api_key": "FMP_API_KEY",
        "fred_api_key": "FRED_API_KEY",
        "finnhub_api_key": "FINNHUB_API_KEY",
        "noaa_cdo_token": "NOAA_CDO_TOKEN",
        "usda_nass_api_key": "USDA_NASS_API_KEY",
    }
    for key, env_name in credentials.items():
        ar[key] = ar.get(key) or os.environ.get(env_name, "")
    if ar.get("equity_universe_policy"):
        ar["equity_universe_credentials"] = {
            "key": os.environ.get("ALPACA_API_KEY", "").strip(),
            "secret": os.environ.get("ALPACA_SECRET_KEY", "").strip(),
        }
    omitted = {"state_dir", "source_cache_dir", "source_cache_ttl_s"}
    if exclude_horizon:
        omitted.add("horizon")
    effective = dict(config)
    effective["autoresearch"] = {k: v for k, v in ar.items() if k not in omitted}
    return configuration_fingerprint(effective)


def public_source_configuration_fingerprint(config: Mapping[str, Any]) -> str:
    """Bind effective frozen behavior without reading or retaining credentials."""
    effective = dict(config)
    omitted = _SOURCE_CREDENTIALS | {"state_dir", "source_cache_dir", "source_cache_ttl_s"}
    effective["autoresearch"] = {
        k: v for k, v in config.get("autoresearch", {}).items() if k not in omitted
    }
    return configuration_fingerprint(effective)


def public_source_configuration_declaration(config: Mapping[str, Any]) -> dict[str, Any]:
    """Generation-bound, secret-free configuration declaration."""
    return {
        PUBLIC_CONFIGURATION_KEY: {
            "version": PUBLIC_CONFIGURATION_VERSION,
            "configuration_sha256": public_source_configuration_fingerprint(config),
        }
    }


def registered_source_fingerprint(configuration: str, source: Any) -> str:
    settings: dict[str, Any] = {"configuration": configuration}
    if source is not None:
        settings["adapter"] = type(source).__module__ + "." + type(source).__qualname__
        settings["credentials"] = {
            key: getattr(source, key)
            for key in ("_api_key", "_token", "_fmp_api_key", "_user_agent")
            if isinstance(getattr(source, key, None), str)
        }
    return configuration_fingerprint(settings)


def cache_identity(source: str, start: str, session: str, configuration: str) -> dict[str, str]:
    date.fromisoformat(start)
    date.fromisoformat(session)
    return {
        "source": source,
        "window_start": start,
        "session": session,
        "configuration": configuration,
        "contract": CONTRACT_VERSION,
    }


def _declared_incomplete(coverage: Any) -> bool:
    if isinstance(coverage, Mapping):
        return (
            ("complete" in coverage and coverage["complete"] is not True)
            or coverage.get("status") in ("failed", "partial", "incomplete", "unavailable")
            or any(_declared_incomplete(v) for v in coverage.values())
        )
    return isinstance(coverage, (list, tuple)) and any(_declared_incomplete(v) for v in coverage)


def successful_source(payload: Any) -> bool:
    if not isinstance(payload, Mapping) or payload.get("error") not in (None, ""):
        return False
    coverage = payload.get("_coverage")
    if "_coverage" in payload and (
        not isinstance(coverage, Mapping)
        or coverage.get("status") not in ("ok", "success", "success_empty", "complete")
    ):
        return False
    if "coverage" in payload and (
        not isinstance(payload["coverage"], Mapping)
        or _declared_incomplete(payload["coverage"])
    ):
        return False
    return True


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise SourceInputError("acquisition time must be timezone-aware")
    return value.astimezone(timezone.utc)


class SourceInputStore:
    """Parquet + plain JSON provider cache and immutable frozen session bundle store."""

    def __init__(
        self,
        cache_dir: str | Path,
        *,
        accepted_dir: str | Path | None = None,
        ttl_s: float = 300,
        max_bytes: int | None = None,
        max_nodes: int | None = None,
    ) -> None:
        _ = max_nodes  # Eliminated: Parquet storage has no structural node cap.
        if not math.isfinite(float(ttl_s)) or not 0 < float(ttl_s) <= 300:
            raise SourceInputError("cache ttl must be between zero and 300 seconds")
        self.cache_dir = Path(cache_dir)
        self.accepted_dir = Path(accepted_dir) if accepted_dir is not None else None
        self.ttl_s = float(ttl_s)
        self._max_bytes = MAX_BYTES if max_bytes is None else int(max_bytes)
        if self._max_bytes < 1:
            raise SourceInputError("invalid max_bytes")
        self._codec_limits: dict[str, int | None] = {"max_bytes": max_bytes}

    @property
    def codec_limits(self) -> dict[str, int | None]:
        return dict(self._codec_limits)

    @staticmethod
    def encode(payload: Any, *, max_bytes: int | None = None, max_nodes: int | None = None) -> str:
        _ = max_nodes
        limit = MAX_BYTES if max_bytes is None else int(max_bytes)
        try:
            plain = _to_plain(payload, depth=0)
            encoded = json.dumps(plain, allow_nan=False, separators=(",", ":"), sort_keys=True)
            if len(encoded.encode("utf-8")) > limit:
                raise SourceInputError("source document exceeds byte limit")
            return encoded
        except SourceInputError:
            raise
        except (ValueError, TypeError, OverflowError, RecursionError) as error:
            raise SourceInputError("invalid source document") from error

    @staticmethod
    def decode(
        encoded: str,
        *,
        max_bytes: int | None = None,
        max_nodes: int | None = None,
        table_dir: Path | None = None,
    ) -> Any:
        _ = max_nodes
        limit = MAX_BYTES if max_bytes is None else int(max_bytes)
        try:
            if not isinstance(encoded, str) or len(encoded.encode("utf-8")) > limit:
                raise SourceInputError("source document exceeds byte limit")
            parsed = load_json_strict(encoded, max_bytes=limit)
            return _from_plain(parsed, depth=0, table_dir=table_dir)
        except SourceInputError:
            raise
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError, AttributeError) as error:
            raise SourceInputError("invalid source document") from error

    def _path(self, identity: Mapping, *, frozen: bool) -> Path:
        if frozen:
            if self.accepted_dir is None:
                raise SourceInputError("accepted source directory not configured")
            slot: Mapping = {"generation": identity["generation"], "session": identity["session"]}
            root = self.accepted_dir
        else:
            slot, root = identity, self.cache_dir
        return root / (configuration_fingerprint(slot) + ".json")

    def _table_dir_for(self, envelope_path: Path) -> Path:
        return envelope_path.parent / (envelope_path.stem + ".tables")

    def _read(self, path: Path, identity: Mapping) -> dict[str, Any]:
        table_dir = self._table_dir_for(path)
        try:
            raw_doc = load_json_strict(path, max_bytes=self._max_bytes)
        except FileNotFoundError:
            raise
        except (OSError, ValueError) as error:
            raise SourceInputError(f"invalid source envelope: {error}") from error
        if not isinstance(raw_doc, dict) or set(raw_doc) != {
            "version",
            "identity",
            "acquired_at",
            "payload",
            "digest",
        }:
            raise SourceInputError("invalid source envelope")
        if raw_doc["version"] != CONTRACT_VERSION or raw_doc["identity"] != dict(identity):
            raise SourceInputError("source identity mismatch")
        payload_json = json.dumps(
            raw_doc["payload"], allow_nan=False, separators=(",", ":"), sort_keys=True
        )
        if raw_doc["digest"] != hashlib.sha256(payload_json.encode("utf-8")).hexdigest():
            raise SourceInputError("source payload digest mismatch")
        acquired = _utc(datetime.fromisoformat(raw_doc["acquired_at"]))
        payload = _from_plain(raw_doc["payload"], depth=0, table_dir=table_dir)
        return {
            "version": raw_doc["version"],
            "identity": raw_doc["identity"],
            "acquired_at": acquired,
            "payload": payload,
            "digest": raw_doc["digest"],
        }

    def _write(
        self,
        path: Path,
        identity: Mapping,
        payload: Any,
        acquired_at: datetime,
        *,
        exclusive: bool,
        deadline: float | None = None,
    ) -> None:
        _check_deadline(deadline)
        table_dir = self._table_dir_for(path)
        plain_payload = _to_plain(payload, depth=0, table_dir=table_dir, table_counter=[0])
        payload_json = json.dumps(
            plain_payload, allow_nan=False, separators=(",", ":"), sort_keys=True
        )
        digest = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        envelope = {
            "version": CONTRACT_VERSION,
            "identity": dict(identity),
            "acquired_at": _utc(acquired_at).isoformat(),
            "payload": plain_payload,
            "digest": digest,
        }
        encoded = json.dumps(envelope, allow_nan=False, separators=(",", ":"), sort_keys=True)
        if len(encoded.encode("utf-8")) > self._max_bytes:
            raise SourceInputError("source document exceeds byte limit")
        _check_deadline(deadline)
        path.parent.mkdir(parents=True, exist_ok=True)
        if exclusive:
            fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".source-")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(tmp_name, 0o600)
                _check_deadline(deadline)
                os.link(tmp_name, path)
                dir_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                except OSError:
                    pass
                finally:
                    os.close(dir_fd)
            finally:
                Path(tmp_name).unlink(missing_ok=True)
        else:
            atomic_write_text(path, encoded, mode=0o600)
        _check_deadline(deadline)

    def load_cached(
        self, identity: Mapping, *, now: datetime | None = None, cutoff: datetime | None = None
    ) -> Any | None:
        now = _utc(now or datetime.now(timezone.utc))
        cutoff = _utc(cutoff or now)
        try:
            document = self._read(self._path(identity, frozen=False), identity)
            acquired = document["acquired_at"]
            if acquired > min(now, cutoff) or (now - acquired).total_seconds() > self.ttl_s:
                return None
            return document["payload"] if successful_source(document["payload"]) else None
        except (OSError, SourceInputError):
            return None

    def save_cached(
        self, identity: Mapping, payload: Any, *, acquired_at: datetime | None = None
    ) -> bool:
        if not successful_source(payload):
            return False
        self._write(
            self._path(identity, frozen=False),
            identity,
            payload,
            acquired_at or datetime.now(timezone.utc),
            exclusive=False,
        )
        return True

    def load_frozen(self, identity: Mapping) -> Any | None:
        try:
            document = self._read(self._path(identity, frozen=True), identity)
            if document["acquired_at"] > datetime.now(timezone.utc):
                raise SourceInputError("future acquisition time")
            return document["payload"]
        except FileNotFoundError:
            return None
        except (OSError, SourceInputError) as error:
            raise SourceInputError(f"frozen source bundle rejected: {error}") from error

    def freeze(
        self,
        identity: Mapping,
        payload: Any,
        *,
        acquired_at: datetime | None = None,
        deadline: float | None = None,
    ) -> Any:
        _check_deadline(deadline)
        existing = self.load_frozen(identity)
        _check_deadline(deadline)
        if existing is not None:
            return existing
        path = self._path(identity, frozen=True)
        try:
            self._write(
                path,
                identity,
                payload,
                acquired_at or datetime.now(timezone.utc),
                exclusive=True,
                deadline=deadline,
            )
        except FileExistsError:
            pass
        result = self.load_frozen(identity)
        _check_deadline(deadline)
        return result


def daily_source_store(owner: Any, session: str) -> tuple[SourceInputStore, dict[str, str]]:
    """Locate accepted inputs using the orchestrator's bound generation identity."""
    config = owner._base_config
    ar = config.get("autoresearch", {})
    generation = owner._metric_epoch_context
    identity = {
        "generation": generation.generation_id,
        "session": session,
        "commit": generation.generation_commit,
        "configuration": source_configuration_fingerprint(config),
    }
    state_dir = Path(ar.get("state_dir", "data/state"))
    cache_dir = (
        ar.get("source_cache_dir")
        or os.environ.get("EVENTEDGE_SOURCE_CACHE_DIR")
        or state_dir / "source_cache"
    )
    return (
        SourceInputStore(
            cache_dir,
            accepted_dir=state_dir / "source_inputs",
            ttl_s=ar.get("source_cache_ttl_s", 300),
        ),
        identity,
    )


def daily_volatility_store(owner: Any, session: str) -> tuple[SourceInputStore, dict[str, str]]:
    """Keep accepted staging history separate from the earlier screening inputs."""
    shared, identity = daily_source_store(owner, session)
    identity = {**identity, "purpose": "staging-volatility-v1"}
    return (
        SourceInputStore(
            shared.cache_dir,
            accepted_dir=shared.accepted_dir / "staging_volatility",
            ttl_s=shared.ttl_s,
        ),
        identity,
    )
