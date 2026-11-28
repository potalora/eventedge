"""Installed only in each acceptance venv, including all actual child processes.

The substitutions are external HTTP/SDK responses and one wall-clock source.
The real model budget, transport subprocess, plan authentication/stream parser,
source acquisition, calendar guards, orchestration and financial books remain.
"""
from __future__ import annotations

import datetime as clock_module
import hashlib
import importlib
import json
import os
from pathlib import Path
import socket
import sys
import time
from types import ModuleType

REAL_DATETIME = clock_module.datetime
SYMBOLS = ['SPY', 'BIL', 'SHY', 'TLT', 'VTI', 'VT', 'DBA', 'AAPL', 'MSFT', 'GOOGL', 'AMZN', 'META', 'NVDA', 'TSLA', 'JPM']


def scenario():
    return json.loads(Path(os.environ['EVENTEDGE_ACCEPTANCE_SCENARIO']).read_text())


def trace(kind, **details):
    row = {'kind': kind, 'pid': os.getpid(), 'ppid': os.getppid(), 'argv': sys.argv, **details}
    # One O_APPEND write, so records from independent real workers cannot mix.
    descriptor = os.open(os.environ['EVENTEDGE_ACCEPTANCE_TRACE'], os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(descriptor, (json.dumps(row, sort_keys=True) + '\n').encode())
    finally:
        os.close(descriptor)


def _forbidden(*args, **kwargs):
    trace('forbidden_network')
    raise AssertionError('scheduled acceptance forbids all network sockets')


def _forbidden_native(*args, **kwargs):
    trace('forbidden_network', boundary='native_curl')
    raise AssertionError('scheduled acceptance forbids native curl network transport')


def _model_text(*, model, system, prompt, effort):
    if 'portfolio manager' in system:
        section = prompt.split('Signals (all ', 1)[1].split('Current positions', 1)[0]
        admitted = [json.loads(line[line.index('{'):]) for line in section.splitlines() if line.startswith('  ') and '{' in line]
        chosen = next(
            (row for row in admitted if row.get('ticker') == 'NVDA' and row.get('strategy') == 'earnings_call' and row.get('direction') == 'long'),
            None,
        )
        value = (
            []
            if chosen is None
            else [
                {
                    'ticker': 'NVDA',
                    'direction': 'long',
                    'position_size_pct': 0.05,
                    'confidence': 0.9,
                    'rationale': 'Constructed quarterly earnings and outlook surprise',
                    'contributing_strategies': ['earnings_call'],
                    'regime_alignment': 'aligned',
                }
            ]
        )
        role = 'committee'
    else:
        value = {
            'direction': 'long',
            'conviction': 0.9,
            'rationale': 'Quarterly earnings exceed forecast with raised guidance',
            'evidence_claim': 'Quarterly earnings exceed forecast with raised guidance',
        }
        role = 'analysis'
    trace('model', role=role, model=model, effort=effort, session=scenario()['session'])
    return json.dumps(value)


def install():
    if 'EVENTEDGE_ACCEPTANCE_SCENARIO' not in os.environ:
        return
    # Install denial before importing anything capable of provider discovery.
    socket.socket.connect = _forbidden
    socket.socket.connect_ex = _forbidden
    socket.create_connection = _forbidden
    socket.getaddrinfo = _forbidden
    try:
        # curl_cffi calls libcurl directly and bypasses Python socket hooks.
        # Deny both native entrypoints before importing Yahoo or other SDKs.
        import curl_cffi

        curl_cffi.Curl.perform = _forbidden_native
        curl_cffi.AsyncCurl.add_handle = _forbidden_native
        _install()
    except BaseException:
        import traceback

        traceback.print_exc()
        # sitecustomize normally swallows errors; acceptance must fail closed.
        os._exit(86)


def _install():
    sys.path.insert(0, os.environ['EVENTEDGE_ACCEPTANCE_WORKTREE'])
    from test_source_reliability_pipeline import TransportFixture
    import pandas as pd
    import requests
    import httpx
    import yfinance

    # The adapter retains its optional SDK availability probe; acquisition is
    # nevertheless the real raw annual-archive HTTP/parser path below.
    sys.modules['cot_reports'] = ModuleType('cot_reports')
    fixture = TransportFixture()
    current = scenario()
    fixture.session = clock_module.date.fromisoformat(current['session'])
    fixture.now = REAL_DATETIME.fromisoformat(current['now'])

    class DatetimeMeta(type):
        def __instancecheck__(cls, value):
            return isinstance(value, REAL_DATETIME)

    class FixedDatetime(REAL_DATETIME, metaclass=DatetimeMeta):
        @classmethod
        def now(cls, tz=None):
            instant = REAL_DATETIME.fromisoformat(scenario()['now'])
            return cls.fromtimestamp(instant.timestamp(), tz) if tz else cls.fromtimestamp(instant.timestamp())

        @classmethod
        def utcnow(cls):
            return cls.now(clock_module.timezone.utc).replace(tzinfo=None)

    # All later imports, including __main__ controller/worker imports, see the
    # same wall clock. Calendar functions and guards themselves stay real.
    clock_module.datetime = FixedDatetime
    for module in list(sys.modules.values()):
        if module is not None and getattr(module, '__name__', '').startswith('tradingagents'):
            if getattr(module, 'datetime', None) is REAL_DATETIME:
                module.datetime = FixedDatetime

    def request(session, method, url, *, _yearly=False, **kwargs):
        current = scenario()
        fault = current.get('fault')
        params = kwargs.get('params') or {}
        safe_params = {key: value for key, value in params.items() if key not in {'api_key', 'token', 'key'}}
        boundary = (
            {'fixture_boundary': 'noaa_yearly_original_lines', 'fixture_original_acquisition_date': '2026-10-19'}
            if _yearly
            else {}
        )
        trace('http', method=method, url=url, params=safe_params, **boundary)
        if fault == 'stall_transport' or (
            fault == 'stall_transport_worker' and any('transport_worker.py' in arg for arg in sys.argv)
        ):
            trace('stalled_transport', url=url)
            time.sleep(120)
        if fault == 'midday_universe_503' and url.endswith('/v2/assets'):
            return fixture.response({}, 503, headers={'Retry-After': '0'})
        if fault == 'midday_usaspending_503' and 'api.usaspending.gov' in url:
            return fixture.response({}, 503, headers={'Retry-After': '0'})
        if url == 'https://api.finra.org/data/group/otcMarket/name/consolidatedShortInterest':
            assert method.lower() == 'post' and kwargs['stream'] is True and kwargs['allow_redirects'] is False
            rows = [
                {
                    'symbolCode': symbol,
                    'issueName': symbol + ' Corporation',
                    'marketClassCode': 'N',
                    'currentShortPositionQuantity': 1000,
                    'previousShortPositionQuantity': 900,
                    'averageDailyVolumeQuantity': 100000,
                    'daysToCoverQuantity': 0.01,
                    'changePercent': 11.1,
                    'changePreviousNumber': 100,
                    'settlementDate': '2026-09-30',
                    'accountingYearMonthNumber': 20260930,
                }
                for symbol in SYMBOLS
            ]
            return fixture.finra_response(kwargs['json'], rows)
        if _yearly:
            names = {
                'https://www.ncei.noaa.gov/pub/data/ghcn/daily/ghcnd-stations.txt': 'stations-ten-state-original.txt',
                'https://www.ncei.noaa.gov/pub/data/ghcn/daily/ghcnd-inventory.txt': 'inventory-ten-state-core-original.txt',
                'https://www.ncei.noaa.gov/pub/data/ghcn/daily/by_year/2026.csv.gz': '2026-ten-state-37day-original.csv.gz',
            }
            assert method == 'GET' and url in names, 'unconfigured NOAA yearly fixture endpoint'
            assert kwargs['stream'] is True and kwargs['allow_redirects'] is False
            body = (Path(__file__).resolve().parents[1] / 'fixtures' / 'noaa_ghcnd_yearly' / names[url]).read_bytes()
            assert len(body) <= kwargs['max_bytes']
            trace(
                'fixture_body',
                boundary='noaa_yearly_original_lines',
                url=url,
                sha256=hashlib.sha256(body).hexdigest(),
                bytes=len(body),
            )
            result = fixture.response(body, headers={'Content-Length': str(len(body))})
            result.url = url
            return result
        if url.endswith('/v2/assets'):
            return fixture.response(
                [{'class': 'us_equity', 'symbol': symbol, 'exchange': 'NASDAQ', 'status': 'active', 'tradable': True} for symbol in SYMBOLS]
            )
        if 'api.stlouisfed.org' in url:
            failed_series = {'fred_optional_payems_503': 'PAYEMS', 'fred_required_hy_503': 'BAMLH0A0HYM2'}.get(fault)
            if params['series_id'] == failed_series:
                return fixture.response({}, 503, headers={'Retry-After': '0'})
            series = fixture.fred(
                params['series_id'],
                observation_start=params['observation_start'],
                observation_end=params['observation_end'],
            )
            observations = [
                {'date': str(day.date()), 'value': str(value), 'realtime_start': '1900-01-01', 'realtime_end': '9999-12-31'}
                for day, value in series.items()
            ]
            return fixture.response(
                {'observations': observations, 'count': len(observations), 'offset': int(params.get('offset', 0)), 'limit': 100000}
            )
        if 'finnhub.io' in url and url.endswith('/calendar/earnings'):
            return fixture.response(
                {
                    'earningsCalendar': [
                        {
                            'symbol': 'NVDA',
                            'date': str(fixture.session),
                            'year': 2026,
                            'quarter': 3,
                            'epsActual': 1.25,
                            'epsEstimate': 1.05,
                            'revenueActual': 46_000_000_000,
                            'revenueEstimate': 44_000_000_000,
                        }
                    ]
                }
            )
        if 'finnhub.io' in url and url.endswith('/company-news'):
            if params.get('symbol') != 'NVDA':
                return fixture.response([])
            return fixture.response(
                [
                    {
                        'id': int(fixture.session.strftime('%Y%m%d')),
                        'headline': 'NVIDIA quarterly results exceed outlook',
                        'summary': 'Revenue and earnings exceeded guidance; management raised its forecast.',
                        'source': 'Reuters',
                        'datetime': int(REAL_DATETIME.combine(fixture.session, clock_module.time(17), clock_module.timezone.utc).timestamp()),
                        'url': 'https://example.test/nvda-results-' + str(fixture.session),
                        'category': 'company',
                    }
                ]
            )
        result = fixture.http(method, url, **kwargs)
        if 'quickstats.nass.usda.gov' in url and params.get('commodity_desc') == 'WHEAT' and int(params['year']) > fixture.session.year:
            # NASS fall winter wheat is labeled with the next harvest year.
            # Spring classes have no such current observations in that year.
            body = result.json()
            body['data'] = [row for row in body['data'] if row['class_desc'] == 'WINTER']
            result = fixture.response(body)
        if 'data.alpaca.markets' in url and '/bars' in url:
            from zoneinfo import ZoneInfo

            requested = clock_module.date.fromisoformat(params['start'][:10])
            stamp = (
                REAL_DATETIME.combine(requested, clock_module.time.min, ZoneInfo('America/New_York'))
                .astimezone(clock_module.timezone.utc)
                .isoformat()
                .replace('+00:00', 'Z')
            )
            body = result.json()
            groups = body['bars'].values() if isinstance(body['bars'], dict) else [body['bars']]
            for rows in groups:
                for row in rows:
                    row['t'] = stamp
            result = fixture.response(body)
        result.url = url
        return result

    requests.sessions.Session.request = request
    noaa_yearly_source = importlib.import_module('tradingagents.strategies.data_sources.noaa_yearly_source')
    noaa_yearly_source._yearly_http_get = lambda url, **kwargs: request(None, 'GET', url, _yearly=True, **kwargs)

    def yahoo(*args, **kwargs):
        url = 'https://query1.finance.yahoo.com/v8/finance/chart'
        trace('sdk', endpoint='yfinance.download', tickers=args[0] if args else kwargs.get('tickers'))
        if scenario().get('fault') == 'stall_transport':
            trace('stalled_transport', url=url)
            time.sleep(120)
        return fixture.yahoo(*args, **kwargs)

    yfinance.download = yahoo

    def earnings_dates(ticker, **kwargs):
        trace('sdk', endpoint='yfinance.Ticker.get_earnings_dates', ticker=ticker.ticker)
        day = REAL_DATETIME.fromisoformat(scenario()['now']) + clock_module.timedelta(days=30)
        return pd.DataFrame({'EPS Estimate': [1.0], 'Reported EPS': [None], 'Surprise(%)': [None]}, index=pd.DatetimeIndex([day]))

    def get_info(ticker, **kwargs):
        trace('sdk', endpoint='yfinance.Ticker.get_info', ticker=ticker.ticker)
        return {
            'symbol': ticker.ticker,
            'sector': 'Technology',
            'industry': 'Semiconductors',
            'marketCap': 1_000_000_000_000,
            'shortName': f'{ticker.ticker} Corporation',
            'longBusinessSummary': 'Constructed external SDK profile',
        }

    yfinance.Ticker.get_earnings_dates = earnings_dates
    yfinance.Ticker.get_info = get_info
    yfinance.Ticker.info = property(get_info)

    def model_response(http_module, request):
        path = request.url.path
        if (
            request.url.host != 'generativelanguage.googleapis.com'
            or not path.startswith('/v1beta/models/')
            or not path.endswith(':generateContent')
        ):
            trace('unexpected_httpx', url=str(request.url))
            raise AssertionError('unconfigured HTTPX endpoint')
        api_key = request.headers.get('x-goog-api-key', '')
        assert isinstance(api_key, str) and api_key.strip(), 'missing x-goog-api-key header'
        assert 'key' not in request.url.params, 'API key must never appear in query string'
        model = path[len('/v1beta/models/'):-len(':generateContent')]
        value = json.loads(request.content)
        gen_cfg = value.get('generationConfig')
        assert isinstance(gen_cfg, dict), 'missing generationConfig'
        assert not ({'temperature', 'topP', 'topK'} & set(gen_cfg)), (
            f'deprecated Gemini 4 sampling params sent: {sorted(gen_cfg)}'
        )
        max_output_tokens = gen_cfg.get('maxOutputTokens')
        assert type(max_output_tokens) is int and max_output_tokens >= 65535, (
            f'maxOutputTokens must be >= 65535, got {max_output_tokens!r}'
        )
        thinking_cfg = gen_cfg.get('thinkingConfig')
        assert isinstance(thinking_cfg, dict), 'missing thinkingConfig'
        assert 'thinkingBudget' not in thinking_cfg, 'deprecated thinkingBudget sent to Gemini 4'
        effort = thinking_cfg.get('thinkingLevel')
        assert effort in {'low', 'medium', 'high', 'xhigh'}, f'invalid thinkingLevel: {effort!r}'
        system_parts = (value.get('systemInstruction') or {}).get('parts') or []
        system = ''.join(part.get('text', '') for part in system_parts if isinstance(part, dict))
        prompt_parts = value['contents'][0]['parts']
        prompt = ''.join(part.get('text', '') for part in prompt_parts if isinstance(part, dict))
        text = _model_text(model=model, system=system, prompt=prompt, effort=effort)
        response = {
            'responseId': 'resp_acceptance',
            'modelVersion': model,
            'candidates': [
                {
                    'finishReason': 'STOP',
                    'content': {
                        'role': 'model',
                        'parts': [
                            {'thought': True, 'text': 'internal acceptance thought'},
                            {'text': text},
                        ],
                    },
                }
            ],
        }
        return http_module.Response(
            200,
            headers={'content-type': 'application/json'},
            content=json.dumps(response).encode(),
            request=request,
        )

    httpx.HTTPTransport.handle_request = lambda transport, request: model_response(httpx, request)
    # Current OpenAI SDKs can use the independently named HTTPX2 transport.
    # Substitute the same physical response boundary, never the SDK request.
    try:
        import httpx2
    except ImportError:
        pass
    else:
        httpx2.HTTPTransport.handle_request = lambda transport, request: model_response(httpx2, request)
    trace(
        'bootstrap',
        network_denied=True,
        now=current['now'],
        ancestor_dotenv_present='EVENTEDGE_ACCEPTANCE_ANCESTOR_CANARY' in os.environ,
    )
    if current.get('preflight_timeout') is not None:
        from tradingagents.strategies.orchestration import generation_manager

        generation_manager._GENERATION_TIMEOUT_S = current['preflight_timeout']
    if current.get('daily_timeout') is not None:
        from tradingagents.strategies.orchestration import runtime_recovery

        runtime_recovery.DAILY_TIMEOUT_SECONDS = current['daily_timeout']
