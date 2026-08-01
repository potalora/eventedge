from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any


def _canonical(value: Any) -> Any:
    if is_dataclass(value):
        return _canonical(asdict(value))
    if isinstance(value, dict):
        return {
            str(key): _canonical(value[key])
            for key in sorted(value, key=lambda item: str(item))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise TypeError(f"Unsupported stable-ID component: {type(value).__name__}")


def stable_id(kind: str, *parts: object) -> str:
    if not kind or not kind.replace("_", "").isalnum():
        raise ValueError("kind must be a non-empty alphanumeric label")
    payload = json.dumps(
        _canonical(parts),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return f"{kind}_{hashlib.sha256(payload).hexdigest()[:32]}"
