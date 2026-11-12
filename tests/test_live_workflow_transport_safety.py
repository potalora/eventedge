import importlib
import pytest



@pytest.fixture(autouse=True)
def isolated_transport_guards(monkeypatch):
    """Every denial test also has a hard no-network fallback if the guard breaks."""
    import socket
    import requests
    import urllib.request
    import curl_cffi
    from curl_cffi import requests as curl_requests
    from tools.live_workflow import transport_safety as safety
    targets = [(requests.sessions.Session, 'request'), (requests.sessions.Session, 'send'),
               (requests.adapters.HTTPAdapter, 'send'), (urllib.request.OpenerDirector, 'open'),
               (curl_requests.Session, 'request'), (curl_requests.AsyncSession, 'request'),
               (curl_cffi.Curl, 'setopt'), (curl_cffi.Curl, 'perform'), (curl_cffi.AsyncCurl, 'add_handle')]
    for name in ('httpx', 'httpx2'):
        module = importlib.import_module(name)
        targets += [(cls, method) for cls in (module.Client, module.AsyncClient)
                    for method in ('send', '_send_single_request')]
    for owner, name in targets:
        monkeypatch.setattr(owner, name, getattr(owner, name))
    monkeypatch.setattr(safety, '_INSTALLED', False)
    monkeypatch.setattr(safety, '_OFFLINE', False)
    def network_forbidden(*args, **kwargs):
        raise AssertionError('denial test reached native network boundary')
    monkeypatch.setattr(socket.socket, 'connect', network_forbidden)
    monkeypatch.setattr(curl_cffi.Curl, 'perform', network_forbidden)
    monkeypatch.setattr(curl_cffi.AsyncCurl, 'add_handle', network_forbidden)
    yield

def test_broker_mutations_denied_sources_allowed():
    from tools.live_workflow.transport_safety import check_request, TransportDenied
    for method in ['POST', 'DELETE', 'PATCH', 'PUT']:
        with pytest.raises(TransportDenied):
            check_request(method, 'https://paper-api.alpaca.markets/v2/orders')
    check_request('GET', 'https://paper-api.alpaca.markets/v2/account')
    check_request('POST', 'https://api.finra.org/data/group/otcMarket/name/otcDailyShortSaleVolume')
    check_request('POST', 'https://api.usaspending.gov/api/v2/search/spending_by_award/')


def test_unclassified_curl_fails_closed():
    from tools.live_workflow.transport_safety import install_transport_safety, TransportDenied
    import curl_cffi
    install_transport_safety()
    curl = curl_cffi.Curl()
    try:
        with pytest.raises(TransportDenied, match='unclassified'):
            curl.perform()
    finally:
        curl.close()


@pytest.mark.parametrize('family', ['requests', 'urllib', 'httpx', 'httpx2', 'curl'])
def test_actual_entrypoints_deny_without_network(family):
    from tools.live_workflow.transport_safety import install_transport_safety, TransportDenied
    install_transport_safety()
    with pytest.raises(TransportDenied):
        if family == 'requests':
            import requests
            requests.post('https://paper-api.alpaca.markets/v2/orders')
        elif family == 'urllib':
            import urllib.request
            urllib.request.urlopen(urllib.request.Request('https://paper-api.alpaca.markets/v2/orders', data=b'{}'))
        elif family in {'httpx', 'httpx2'}:
            module = importlib.import_module(family)
            with module.Client() as client:
                client.post('https://paper-api.alpaca.markets/v2/orders')
        else:
            from curl_cffi import requests
            requests.post('https://paper-api.alpaca.markets/v2/orders')


@pytest.mark.parametrize('family', ['httpx', 'httpx2', 'curl'])
def test_async_broker_writes_denied(family):
    import asyncio
    from tools.live_workflow.transport_safety import install_transport_safety, TransportDenied
    install_transport_safety()
    async def call():
        if family == 'curl':
            from curl_cffi.requests import AsyncSession
            async with AsyncSession() as client:
                await client.delete('https://paper-api.alpaca.markets/v2/positions')
        else:
            module = importlib.import_module(family)
            async with module.AsyncClient() as client:
                await client.delete('https://paper-api.alpaca.markets/v2/positions')
    with pytest.raises(TransportDenied):
        asyncio.run(call())


def test_query_posts_reach_controlled_httpx_transport():
    import httpx
    from tools.live_workflow.transport_safety import install_transport_safety
    install_transport_safety()
    seen = []
    def handle(request):
        seen.append((request.method, request.url.host))
        return httpx.Response(200, json={'rows': []})
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        for host in ['api.finra.org', 'api.usaspending.gov']:
            assert client.post('https://' + host + '/query', json={'symbols': ['A']}).status_code == 200
    assert seen == [('POST', 'api.finra.org'), ('POST', 'api.usaspending.gov')]


def test_direct_requests_adapter_cannot_bypass_broker_guard():
    import requests
    from tools.live_workflow.transport_safety import install_transport_safety, TransportDenied
    install_transport_safety()
    request = requests.Request('DELETE', 'https://paper-api.alpaca.markets/v2/orders').prepare()
    with pytest.raises(TransportDenied):
        requests.adapters.HTTPAdapter().send(request)


def test_exact_yahoo_consent_post_reaches_native_boundary(monkeypatch):
    import curl_cffi
    from curl_cffi.requests import Session
    from tools.live_workflow.transport_safety import install_transport_safety
    class BoundaryReached(Exception):
        pass
    def native(self, *args, **kwargs):
        raise BoundaryReached()
    monkeypatch.setattr(curl_cffi.Curl, 'perform', native)
    install_transport_safety()
    with Session() as client:
        with pytest.raises(BoundaryReached):
            client.post('https://consent.yahoo.com/v2/collectConsent?sessionId=SENTINEL', data={'csrfToken': 'SENTINEL'})


def test_yahoo_consent_redirect_guard_preserves_headers_and_denies_broker():
    from io import BytesIO
    from types import SimpleNamespace
    from tools.live_workflow.transport_safety import _ConsentHeaderBuffer, TransportDenied
    handle = SimpleNamespace(_acceptance_url='https://consent.yahoo.com/v2/collectConsent', _acceptance_method='POST')
    target = BytesIO()
    guarded = _ConsentHeaderBuffer(handle, target)
    assert guarded.write(b'HTTP/2 303 See Other\r\n') == len(b'HTTP/2 303 See Other\r\n')
    guarded.write(b'Location: https://guce.yahoo.com/copyConsent?sessionId=SENTINEL\r\n')
    assert target.getvalue().endswith(b'SENTINEL\r\n')
    guarded.write(b'HTTP/2 307 Temporary Redirect\r\n')
    before = target.getvalue()
    with pytest.raises(TransportDenied):
        guarded.write(b'Location: https://paper-api.alpaca.markets/v2/orders\r\n')
    assert target.getvalue() == before


def test_installed_curl_header_callback_blocks_consent_broker_redirect(monkeypatch):
    import curl_cffi
    from curl_cffi.requests import Session
    from tools.live_workflow.transport_safety import install_transport_safety, TransportDenied
    def native(self, *args, **kwargs):
        callback = curl_cffi.ffi.from_handle(self._header_handle).callback
        callback.write(b'HTTP/2 307 Temporary Redirect\r\n')
        callback.write(b'Location: https://paper-api.alpaca.markets/v2/orders\r\n')
        raise AssertionError('broker redirect was allowed')
    monkeypatch.setattr(curl_cffi.Curl, 'perform', native)
    install_transport_safety()
    with Session() as client:
        with pytest.raises(TransportDenied, match='consent redirect denied'):
            client.post('https://consent.yahoo.com/v2/collectConsent', data={'csrfToken': 'SENTINEL'})
