"""Safe failure boundary between source adapters and their consumers."""

from __future__ import annotations

import json
import re
from itertools import islice
from typing import Any
from urllib.error import HTTPError, URLError

import requests

_REASON_CODES = frozenset(
    {
        "timeout",
        "transport_error",
        "http_error",
        "invalid_response",
        "provider_error",
        "batch_failure",
    }
)


class SourceFetchError(RuntimeError):
    """A fixed diagnostic, bounded failure identities, and valid partial data.

    Messages are constants supplied by adapters, never raw provider exceptions.
    Operation names are sanitized before they enter health or report payloads.
    """

    def __init__(
        self,
        message: str,
        *,
        reason_code: str = "provider_error",
        http_status: int | None = None,
        failed_operations: dict[str, str] | None = None,
        failed_http_statuses: dict[str, int] | None = None,
        partial_data: dict[str, Any] | None = None,
    ):
        if reason_code not in _REASON_CODES:
            raise ValueError("unknown source failure reason")
        self.reason_code = reason_code
        self.http_status = (
            http_status
            if type(http_status) is int and 100 <= http_status <= 599
            else None
        )
        self.failed_operations = {}
        self.failed_http_statuses = {}
        for operation, reason in islice((failed_operations or {}).items(), 16):
            safe_operation = (
                operation
                if isinstance(operation, str)
                and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", operation)
                else "unknown"
            )
            self.failed_operations[safe_operation] = (
                reason if reason in _REASON_CODES else "provider_error"
            )
            status = (failed_http_statuses or {}).get(operation)
            if type(status) is int and 100 <= status <= 599:
                self.failed_http_statuses[safe_operation] = status
        details = [reason_code]
        if self.http_status is not None:
            details.append(f"http_status={self.http_status}")
        for operation, reason in sorted(self.failed_operations.items()):
            detail = f"{operation}:{reason}"
            if operation in self.failed_http_statuses:
                detail += f":http_status={self.failed_http_statuses[operation]}"
            details.append(detail)
        super().__init__(f"{message} [{'; '.join(details)}]")
        self.partial_data = partial_data if partial_data is not None else {}


def source_fetch_error(message: str, error: Exception) -> SourceFetchError:
    """Classify bounded exception chains by type/status, never message or URL.

    fredapi replaces HTTPError with ValueError or XML ParseError while retaining
    the HTTPError as context. Inspect at most eight nodes and reject cycles.
    """
    current: BaseException | None = error
    seen: set[int] = set()
    for _ in range(8):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        if isinstance(current, SourceFetchError):
            return current
        if isinstance(current, (TimeoutError, requests.Timeout)) or (
            isinstance(current, URLError) and isinstance(current.reason, TimeoutError)
        ):
            return SourceFetchError(message, reason_code="timeout")
        if isinstance(current, (HTTPError, requests.HTTPError)):
            status = (
                current.code
                if isinstance(current, HTTPError)
                else getattr(current.response, "status_code", None)
            )
            return SourceFetchError(
                message, reason_code="http_error", http_status=status
            )
        if isinstance(
            current, (json.JSONDecodeError, requests.exceptions.JSONDecodeError)
        ):
            return SourceFetchError(message, reason_code="invalid_response")
        if isinstance(current, (requests.RequestException, URLError, OSError)):
            return SourceFetchError(message, reason_code="transport_error")
        current = current.__cause__ or current.__context__
    return SourceFetchError(message, reason_code="provider_error")
