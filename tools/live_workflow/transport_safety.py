"""Independent outbound write guards, installed only in the acceptance runtime.

These guards are defense in depth, not a socket sandbox. Offline execution also
requires an OS network namespace. Native curl handles must originate in a guarded
session; direct unclassified Curl.perform/add_handle operations fail closed.
"""
from __future__ import annotations

import contextvars
import functools
import importlib
from urllib.parse import urlsplit, urljoin


class TransportDenied(RuntimeError):
    pass


_READ_METHODS = frozenset({'GET', 'HEAD', 'OPTIONS'})
_BROKER_DOMAINS = ('alpaca.markets', 'tradier.com', 'interactivebrokers.com', 'ibkr.com')
_CURL_SESSION = contextvars.ContextVar('acceptance_curl_session', default=None)
_INSTALLED = False
_OFFLINE = False


def check_request(method, url):
    """Reject broker writes regardless of paper profile and before transport IO."""
    parsed = urlsplit(str(url))
    host = (parsed.hostname or '').lower().rstrip('.')
    if parsed.scheme not in {'http', 'https'} or not host or parsed.username or parsed.password:
        raise TransportDenied('unsupported outbound URL')
    if _OFFLINE:
        raise TransportDenied('offline network denied')
    if str(method).upper() not in _READ_METHODS and any(host == d or host.endswith('.' + d) for d in _BROKER_DOMAINS):
        raise TransportDenied('broker mutation denied')



def _is_yahoo_consent(method, url):
    parsed = urlsplit(str(url))
    return (str(method).upper() == 'POST' and parsed.scheme == 'https'
            and parsed.hostname == 'consent.yahoo.com' and parsed.port in (None, 443)
            and parsed.path == '/v2/collectConsent' and not parsed.username and not parsed.password)


def _check_consent_header(handle, data):
    """Validate consent redirects in libcurl's existing header callback.

    This runs before libcurl follows the Location. Ordinary Yahoo HTTPS redirects
    retain SDK behavior; no follow_redirects setting is changed.
    """
    if not _is_yahoo_consent(getattr(handle, '_acceptance_method', ''), getattr(handle, '_acceptance_url', '')):
        return
    if data.startswith(b'HTTP/'):
        parts = data.split(None, 2)
        handle._acceptance_redirect_response = len(parts) > 1 and parts[1] in {b'301', b'302', b'303', b'307', b'308'}
    elif getattr(handle, '_acceptance_redirect_response', False) and data.lower().startswith(b'location:'):
        location = data.split(b':', 1)[1].strip().decode('latin-1')
        target = urljoin(getattr(handle, '_acceptance_current_url', handle._acceptance_url), location)
        parsed = urlsplit(target)
        host = (parsed.hostname or '').lower()
        if (parsed.scheme != 'https' or parsed.port not in (None, 443) or parsed.username or parsed.password
                or not (host == 'yahoo.com' or host.endswith('.yahoo.com'))):
            raise TransportDenied('native Yahoo consent redirect denied')
        handle._acceptance_current_url = target


class _ConsentHeaderBuffer:
    def __init__(self, handle, target):
        self.handle, self.target = handle, target
    def __getattr__(self, name):
        return getattr(self.target, name)
    def write(self, data):
        _check_consent_header(self.handle, data)
        return self.target.write(data)

def _request_method(original):
    @functools.wraps(original)
    def guarded(self, method, url, *args, **kwargs):
        check_request(method, url)
        return original(self, method, url, *args, **kwargs)
    return guarded


def _async_request_method(original):
    @functools.wraps(original)
    async def guarded(self, method, url, *args, **kwargs):
        check_request(method, url)
        return await original(self, method, url, *args, **kwargs)
    return guarded


def _send_method(original, asynchronous=False):
    if asynchronous:
        @functools.wraps(original)
        async def guarded(self, request, *args, **kwargs):
            check_request(request.method, str(request.url))
            return await original(self, request, *args, **kwargs)
    else:
        @functools.wraps(original)
        def guarded(self, request, *args, **kwargs):
            check_request(request.method, str(request.url))
            return original(self, request, *args, **kwargs)
    return guarded


def install_transport_safety(*, offline=False):
    global _INSTALLED, _OFFLINE
    _OFFLINE = bool(offline)
    if _INSTALLED:
        return
    import requests
    import urllib.request
    requests.sessions.Session.request = _request_method(requests.sessions.Session.request)
    requests.sessions.Session.send = _send_method(requests.sessions.Session.send)
    requests.adapters.HTTPAdapter.send = _send_method(requests.adapters.HTTPAdapter.send)
    original_open = urllib.request.OpenerDirector.open
    @functools.wraps(original_open)
    def open_guard(self, fullurl, data=None, *args, **kwargs):
        if isinstance(fullurl, urllib.request.Request):
            method, url = fullurl.get_method(), fullurl.full_url
        else:
            method, url = ('POST' if data is not None else 'GET'), fullurl
        check_request(method, url)
        return original_open(self, fullurl, data, *args, **kwargs)
    urllib.request.OpenerDirector.open = open_guard
    for name in ('httpx', 'httpx2'):
        try:
            module = importlib.import_module(name)
        except ImportError:
            continue
        module.Client.send = _send_method(module.Client.send)
        module.AsyncClient.send = _send_method(module.AsyncClient.send, True)
        # Redirect requests are dispatched below send in HTTPX.
        module.Client._send_single_request = _send_method(module.Client._send_single_request)
        module.AsyncClient._send_single_request = _send_method(module.AsyncClient._send_single_request, True)
    try:
        import curl_cffi
        from curl_cffi import requests as curl_requests
        from curl_cffi.const import CurlOpt
    except ImportError:
        _INSTALLED = True
        return

    def session_guard(original, asynchronous=False):
        if asynchronous:
            @functools.wraps(original)
            async def guarded(self, method, url, *args, **kwargs):
                check_request(method, url)
                token = _CURL_SESSION.set(str(method).upper())
                try:
                    return await original(self, method, url, *args, **kwargs)
                finally:
                    _CURL_SESSION.reset(token)
        else:
            @functools.wraps(original)
            def guarded(self, method, url, *args, **kwargs):
                check_request(method, url)
                token = _CURL_SESSION.set(str(method).upper())
                try:
                    return original(self, method, url, *args, **kwargs)
                finally:
                    _CURL_SESSION.reset(token)
        return guarded
    curl_requests.Session.request = session_guard(curl_requests.Session.request)
    curl_requests.AsyncSession.request = session_guard(curl_requests.AsyncSession.request, True)
    original_setopt = curl_cffi.Curl.setopt
    @functools.wraps(original_setopt)
    def setopt_guard(self, option, value):
        if option == CurlOpt.URL:
            self._acceptance_url = value.decode() if isinstance(value, bytes) else str(value)
            self._acceptance_registered = _CURL_SESSION.get() is not None
            self._acceptance_method = _CURL_SESSION.get() or 'GET'
            self._acceptance_header_guard = False
            self._acceptance_current_url = self._acceptance_url
            self._acceptance_redirect_response = False
        elif option == CurlOpt.CUSTOMREQUEST:
            self._acceptance_method = value.decode() if isinstance(value, bytes) else str(value)
        elif option in (CurlOpt.POST, CurlOpt.POSTFIELDS) and value:
            self._acceptance_method = 'POST'
        elif option == CurlOpt.UPLOAD and value:
            self._acceptance_method = 'PUT'
        elif option == CurlOpt.HTTPGET and value:
            self._acceptance_method = 'GET'
        elif option == CurlOpt.HEADERDATA:
            value = _ConsentHeaderBuffer(self, value)
            self._acceptance_header_guard = True
        elif option == CurlOpt.HEADERFUNCTION:
            callback = value
            def header_callback(data):
                _check_consent_header(self, data)
                return callback(data)
            value = header_callback
            self._acceptance_header_guard = True
        return original_setopt(self, option, value)
    curl_cffi.Curl.setopt = setopt_guard

    def validate_handle(handle):
        if not getattr(handle, '_acceptance_registered', False):
            raise TransportDenied('unclassified native curl handle denied')
        method = getattr(handle, '_acceptance_method', 'GET')
        check_request(method, handle._acceptance_url)
        # The reviewed Yahoo SDK needs exactly this consent POST. Its installed
        # header callback guards every redirect before libcurl follows it. Other
        # native writes remain unsupported; broker requests already failed above.
        if method.upper() not in _READ_METHODS:
            if not _is_yahoo_consent(method, handle._acceptance_url) or not getattr(handle, '_acceptance_header_guard', False):
                raise TransportDenied('native curl write/redirect path unsupported')
    original_perform = curl_cffi.Curl.perform
    @functools.wraps(original_perform)
    def perform_guard(self, *args, **kwargs):
        validate_handle(self)
        return original_perform(self, *args, **kwargs)
    curl_cffi.Curl.perform = perform_guard
    original_add = curl_cffi.AsyncCurl.add_handle
    @functools.wraps(original_add)
    def add_guard(self, curl, *args, **kwargs):
        validate_handle(curl)
        return original_add(self, curl, *args, **kwargs)
    curl_cffi.AsyncCurl.add_handle = add_guard
    _INSTALLED = True
