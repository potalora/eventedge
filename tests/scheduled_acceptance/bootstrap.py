"""Installed only in each acceptance venv, including all actual child processes.

The substitutions are external HTTP/SDK responses and one wall-clock source.
The real model budget, transport subprocess, plan authentication/stream parser,
source acquisition, calendar guards, orchestration and financial books remain.
"""
from __future__ import annotations

import datetime as clock_module
import hashlib
import importlib
from importlib import metadata
import json
import os
from pathlib import Path
import socket
import sys
import time
from types import ModuleType, SimpleNamespace

REAL_DATETIME = clock_module.datetime


def scenario():
    return json.loads(Path(os.environ['EVENTEDGE_ACCEPTANCE_SCENARIO']).read_text())


def trace(kind, **details):
    row = {'kind': kind, 'pid': os.getpid(), 'ppid': os.getppid(),
           'argv': sys.argv, **details}
    # One O_APPEND write, so records from independent real workers cannot mix.
    descriptor = os.open(os.environ['EVENTEDGE_ACCEPTANCE_TRACE'], os.O_WRONLY|os.O_CREAT|os.O_APPEND, 0o600)
    try:
        os.write(descriptor, (json.dumps(row, sort_keys=True)+'\n').encode())
    finally:
        os.close(descriptor)


def _forbidden(*args, **kwargs):
    trace('forbidden_network')
    raise AssertionError('scheduled acceptance forbids all network sockets')


def _forbidden_native(*args, **kwargs):
    trace('forbidden_network',boundary='native_curl')
    raise AssertionError('scheduled acceptance forbids native curl network transport')


def _install_openbb():
    """External SDK fixture; internal OpenBB/FINRA adapters stay unmodified."""
    import sqlite3
    from tradingagents.strategies.data_sources.finra_bulk import ALIASES, SCHEMA
    base = Path(os.environ['EVENTEDGE_ACCEPTANCE_BASE'])
    db = base/'finra-response.db'
    with sqlite3.connect(db) as connection:
        connection.execute('CREATE TABLE IF NOT EXISTS short_interest ('+', '.join('"'+key+'" '+kind for key,kind in SCHEMA)+')')
        if not connection.execute('SELECT COUNT(*) FROM short_interest').fetchone()[0]:
            for index,symbol in enumerate(SYMBOLS):
                connection.execute('INSERT INTO short_interest VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                    # Sep30 is the published current cycle at the constructed
                    # Oct14/15 clock; Oct13 is not a FINRA settlement cycle.
                    (index,symbol,symbol+' Corporation','N',1000,900,100000,0.01,11.1,100,'2026-09-30'))
    def profile(symbol, **kwargs):
        trace('sdk', endpoint='openbb.profile', symbols=symbol)
        return SimpleNamespace(results=[SimpleNamespace(symbol=s, sector='Technology',industry_category='Semiconductors',
            market_cap=1e12,name=s+' Corporation',long_description='Constructed external SDK profile') for s in symbol.split(',')])
    def factors(**kwargs):
        trace('sdk', endpoint='openbb.factors')
        fields = dict(date=clock_module.date(2026,9,30),mkt_rf=0.01,smb=0.0,hml=0.0,rf=0.001)
        if kwargs.get('factor') == '5_factors':
            fields.update(rmw=0.0,cma=0.0)
        return SimpleNamespace(results=[SimpleNamespace(**fields)])
    obb = ModuleType('openbb')
    obb.obb = SimpleNamespace(user=SimpleNamespace(credentials=SimpleNamespace()),
        equity=SimpleNamespace(profile=profile),famafrench=SimpleNamespace(factors=factors))
    sys.modules['openbb'] = obb
    storage = ModuleType('openbb_finra.utils.data_storage')
    storage.get_db_path = lambda: str(db)
    storage.get_short_interest_dates = lambda: ['20260915','20260930']
    storage.prepare_data = lambda: trace('sdk',endpoint='finra.prepare_data')
    def download(compact):
        trace('sdk',endpoint='finra.get_data_from_date_and_store',settlement_date=compact)
        # The constructed current response is already cached. Detect accidental
        # history backfill or refresh rather than touching real SDK transport.
        raise AssertionError('Unexpected FINRA archive download in warm-cache fixture')
    storage.get_data_from_date_and_store = download
    class Fetcher:
        @staticmethod
        def transform_query(value):
            return SimpleNamespace(symbol=value['symbol'])
        @staticmethod
        def transform_data(query, rows):
            trace('sdk',endpoint='finra.transform_data',symbols=query.symbol)
            results = []
            for row in rows:
                fields = {key:row[alias] for key,alias in ALIASES.items()}
                fields['settlement_date'] = clock_module.date.fromisoformat(fields['settlement_date'])
                results.append(SimpleNamespace(**fields))
            return results
    model = ModuleType('openbb_finra.models.equity_short_interest')
    model.FinraShortInterestFetcher = Fetcher
    model.FinraShortInterestData = type('FinraShortInterestData',(),{'__alias_dict__':ALIASES})
    for name in ('openbb_finra','openbb_finra.utils','openbb_finra.models'):
        module = ModuleType(name)
        module.__path__ = []
        sys.modules[name] = module
    sys.modules[storage.__name__] = storage
    sys.modules[model.__name__] = model
    original_version = metadata.version
    versions = {'openbb-finra':'1.6.1','openbb-core':'1.6.13'}
    metadata.version = lambda name: versions[name] if name in versions else original_version(name)


SYMBOLS = ['SPY','BIL','SHY','TLT','DBA','AAPL','MSFT','GOOGL','AMZN','META','NVDA','TSLA','JPM','CDRE']


def _recognition(context):
    claims = [
        {'id':'table_shares','printed_value':'100','unit':'shares','row':'Cadre Holdings Inc.','column':'Number of Shares',
         'citation':'Cadre Holdings Inc. Number of Shares 100 shares'},
        {'id':'total_A','printed_value':'200','unit':'securities','row':'total A','column':'Count',
         'citation':'total A Count 200 securities'},
        {'id':'total_C','printed_value':'300','unit':'securities','row':'total C','column':'Count',
         'citation':'total C Count 300 securities'}]
    return {'version':'bounded-document-recognition-v1','document_id':context['document_id'],
        'as_of':context['as_of'],'original_sha256':context['original']['sha256'],
        'request_identity_sha256':context['request_identity_sha256'],
        'pages':[{'page':18,'status':'readable','transcription':'\n'.join(c['citation'] for c in claims),
                  'probes':claims,'uncertainties':[]}]}


def _model_text(request):
    system = request['instructions']
    content = request['input'][0]['content']
    prompt = content if isinstance(content,str) else content[0]['text']
    if 'portfolio manager' in system:
        section = prompt.split('Signals (all ',1)[1].split('Current positions',1)[0]
        admitted = [json.loads(line[line.index('{'):]) for line in section.splitlines() if line.startswith('  ') and '{' in line]
        chosen = next((row for row in admitted if row.get('ticker')=='NVDA' and row.get('strategy')=='earnings_call'
                       and row.get('direction')=='long'),None)
        value = [] if chosen is None else [{'ticker':'NVDA','direction':'long','position_size_pct':.05,
            'confidence':.9,'rationale':'Constructed quarterly earnings and outlook surprise',
            'contributing_strategies':['earnings_call'],'regime_alignment':'aligned'}]
        role = 'committee'
    else:
        try:
            document_prompt = json.loads(prompt[prompt.index('{'):])
        except (ValueError,TypeError):
            document_prompt = None
        graphic_model = None
        if isinstance(document_prompt, dict) and scenario().get('regulation_graphics'):
            from scheduled_acceptance.regulation_graphics_fixture import model
            graphic_model = model(document_prompt, scenario().get('fault'))
        if graphic_model is not None:
            value, role = graphic_model
        elif isinstance(document_prompt,dict) and 'recognition_contract' in document_prompt:
            value = {'recognition':_recognition(document_prompt['recognition_contract']),
                'context_sufficient':True,'material_context_failures':[]}
            role = 'document_verification'
        elif isinstance(document_prompt,dict) and 'recognition_request_version' in document_prompt:
            value = _recognition(document_prompt)
            role = 'document_recognition'
        elif 'Assess ONLY the independently reviewed bounded claims' in system:
            value = {'assessment_scope':'selected_pages','direction':'neutral','score':0.0,
                'rationale':'A historical pledge count provides no current directional catalyst.',
                'evidence_claim':'Selected historical securities counts only.',
                'context_sufficient':True,'used_claim_ids':['table_shares','total_A','total_C']}
            role = 'document_thesis'
        else:
            value = {'direction':'long','conviction':.9,'rationale':'Quarterly earnings exceed forecast with raised guidance',
                     'evidence_claim':'Quarterly earnings exceed forecast with raised guidance'}
            role = 'analysis'
    trace('model', role=role, model=request['model'],effort=request['reasoning']['effort'],session=scenario()['session'])
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
    sys.path.insert(0,os.environ['EVENTEDGE_ACCEPTANCE_WORKTREE'])
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
        def __instancecheck__(cls,value):
            return isinstance(value,REAL_DATETIME)
    class FixedDatetime(REAL_DATETIME,metaclass=DatetimeMeta):
        @classmethod
        def now(cls,tz=None):
            instant = REAL_DATETIME.fromisoformat(scenario()['now'])
            return cls.fromtimestamp(instant.timestamp(),tz) if tz else cls.fromtimestamp(instant.timestamp())
        @classmethod
        def utcnow(cls):
            return cls.now(clock_module.timezone.utc).replace(tzinfo=None)
    # All later imports, including __main__ controller/worker imports, see the
    # same wall clock. Calendar functions and guards themselves stay real.
    clock_module.datetime = FixedDatetime
    for module in list(sys.modules.values()):
        if module is not None and getattr(module,'__name__','').startswith('tradingagents'):
            if getattr(module,'datetime',None) is REAL_DATETIME:
                module.datetime = FixedDatetime
    _install_openbb()
    def request(session, method,url,*,_yearly=False,**kwargs):
        current = scenario()
        fault = current.get('fault')
        params = kwargs.get('params') or {}
        safe_params = {key:value for key,value in params.items() if key not in {'api_key','token','key'}}
        boundary = ({'fixture_boundary':'noaa_yearly_original_lines',
                     'fixture_original_acquisition_date':'2026-10-19'} if _yearly else {})
        trace('http',method=method,url=url,params=safe_params,**boundary)
        if current.get('regulation_graphics'):
            from scheduled_acceptance.regulation_graphics_fixture import response
            graphic_response = response(fixture, url, params, fault)
            if graphic_response is not None:
                return graphic_response
        if fault == 'stall_transport' or (fault == 'stall_transport_worker' and
                any('transport_worker.py' in arg for arg in sys.argv)):
            trace('stalled_transport',url=url)
            time.sleep(120)
        if fault == 'midday_universe_503' and url.endswith('/v2/assets'):
            return fixture.response({},503,headers={'Retry-After':'0'})
        if fault == 'midday_congress_503' and '/revision/main' in url:
            return fixture.response({},503,headers={'Retry-After':'0'})
        if url == 'https://api.finra.org/data/group/otcMarket/name/consolidatedShortInterest':
            # The DB holds constructed external response rows, not a production
            # adapter cache. Exercise the real API transport/parser and receipt.
            import sqlite3
            assert method.lower() == 'post' and kwargs['stream'] is True and kwargs['allow_redirects'] is False
            db = Path(os.environ['EVENTEDGE_ACCEPTANCE_BASE']) / 'finra-response.db'
            with sqlite3.connect(db.as_uri() + '?mode=ro', uri=True) as connection:
                connection.row_factory = sqlite3.Row
                rows = [dict(row) for row in connection.execute('SELECT * FROM short_interest')]
            for row in rows:
                row.pop('index')
                row['accountingYearMonthNumber'] = int(row['settlementDate'].replace('-', ''))
            return fixture.finra_response(kwargs['json'], rows)
        if _yearly:
            # Only this new HTTP boundary uses retained original public bytes.
            # The Oct19 acquisition is replayed under a constructed Oct14/15
            # clock; this establishes no historical point-in-time availability.
            names = {
                'https://www.ncei.noaa.gov/pub/data/ghcn/daily/ghcnd-stations.txt':
                    'stations-ten-state-original.txt',
                'https://www.ncei.noaa.gov/pub/data/ghcn/daily/ghcnd-inventory.txt':
                    'inventory-ten-state-core-original.txt',
                'https://www.ncei.noaa.gov/pub/data/ghcn/daily/by_year/2026.csv.gz':
                    '2026-ten-state-37day-original.csv.gz',
            }
            assert method == 'GET' and url in names, 'unconfigured NOAA yearly fixture endpoint'
            assert kwargs['stream'] is True and kwargs['allow_redirects'] is False
            body = (Path(__file__).resolve().parents[1] / 'fixtures' / 'noaa_ghcnd_yearly' / names[url]).read_bytes()
            assert len(body) <= kwargs['max_bytes']
            trace('fixture_body',boundary='noaa_yearly_original_lines',url=url,
                  sha256=hashlib.sha256(body).hexdigest(),bytes=len(body))
            result = fixture.response(body,headers={'Content-Length':str(len(body))})
            result.url = url
            return result
        if url.endswith('/v2/assets'):
            return fixture.response([{'class':'us_equity','symbol':symbol,'exchange':'NASDAQ',
                'status':'active','tradable':True} for symbol in SYMBOLS])
        if (fault == 'congress_recent_audit_gap'
                and url == 'https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/99999.pdf'):
            # Constructed transport bytes, never an actual official filing or
            # publisher row. The missing publisher ID and audit warning remain.
            assert method.lower() == 'get' and kwargs['stream'] is True and kwargs['allow_redirects'] is False
            body = b'%PDF-1.4\nexplicit synthetic scheduled House original fixture\n%%EOF\n'
            trace('fixture_body',boundary='synthetic_house_original_pdf',url=url,
                  sha256=hashlib.sha256(body).hexdigest(),bytes=len(body))
            result = fixture.response(body,headers={'Content-Length':str(len(body)),'Content-Type':'application/pdf'})
            result.url = url
            return result
        if 'huggingface.co' in url or url.endswith('2026FD.ZIP'):
            from test_congress_disclosure_audit import synthetic_raw
            raw = synthetic_raw()
            snapshot = json.loads(raw['snapshot.json'])
            snapshot['generatedAt'] = current['now']
            snapshot['lastSourceCheckAt'] = current['now']
            if '/revision/main' in url:
                body = json.dumps({'sha':'b10790b'+'0'*33,'id':'austin-starks/congressional-stock-trades'}).encode()
            elif url.endswith('/snapshot.json'):
                body = json.dumps(snapshot).encode()
            elif url.endswith('2026FD.ZIP'):
                body = raw['house-2026FD.ZIP']
                if fault == 'congress_recent_audit_gap':
                    import io, zipfile
                    with zipfile.ZipFile(io.BytesIO(body)) as archive:
                        xml = archive.read('2026FD.xml').replace(b'</FinancialDisclosure>',
                            b'<Member><DocID>99999</DocID><FilingType>P</FilingType>'
                            b'<FilingDate>10/1/2026</FilingDate></Member></FinancialDisclosure>')
                    buffer = io.BytesIO()
                    with zipfile.ZipFile(buffer, 'w') as archive:
                        archive.writestr('2026FD.xml', xml)
                        archive.writestr('2026FD.txt', 'synthetic')
                    body = buffer.getvalue()
            elif '/political_filings/' in url:
                body = raw['filings-2026.parquet']
            elif '/political_trades/' in url:
                body = raw['trades-2026.parquet']
            else:
                raise AssertionError('unconfigured congressional fixture endpoint')
            return fixture.response(body)
        if 'api.stlouisfed.org' in url:
            failed_series = {'fred_optional_payems_503': 'PAYEMS',
                             'fred_required_hy_503': 'BAMLH0A0HYM2'}.get(fault)
            if params['series_id'] == failed_series:
                return fixture.response({}, 503, headers={'Retry-After': '0'})
            series = fixture.fred(params['series_id'],observation_start=params['observation_start'],observation_end=params['observation_end'])
            observations = [{'date':str(day.date()),'value':str(value),'realtime_start':'1900-01-01','realtime_end':'9999-12-31'}
                            for day,value in series.items()]
            return fixture.response({'observations':observations,'count':len(observations),
                'offset':int(params.get('offset',0)),'limit':100000})
        if 'finnhub.io' in url and url.endswith('/calendar/earnings'):
            return fixture.response({'earningsCalendar':[{'symbol':'NVDA','date':str(fixture.session),
                'year':2026,'quarter':3,'epsActual':1.25,'epsEstimate':1.05,
                'revenueActual':46_000_000_000,'revenueEstimate':44_000_000_000}]})
        if 'finnhub.io' in url and url.endswith('/company-news'):
            if params.get('symbol')!='NVDA':
                return fixture.response([])
            return fixture.response([{'id':int(fixture.session.strftime('%Y%m%d')),
                'headline':'NVIDIA quarterly results exceed outlook','summary':'Revenue and earnings exceeded guidance; management raised its forecast.',
                'source':'Reuters','datetime':int(REAL_DATETIME.combine(fixture.session,clock_module.time(17),clock_module.timezone.utc).timestamp()),
                'url':'https://example.test/nvda-results-'+str(fixture.session),'category':'company'}])
        result = fixture.http(method,url,**kwargs)
        if 'quickstats.nass.usda.gov' in url and params.get('commodity_desc')=='WHEAT' and int(params['year'])>fixture.session.year:
            # NASS fall winter wheat is labeled with the next harvest year.
            # Spring classes have no such current observations in that year.
            body = result.json()
            body['data'] = [row for row in body['data'] if row['class_desc']=='WINTER']
            result = fixture.response(body)
        if 'data.alpaca.markets' in url and '/bars' in url:
            from zoneinfo import ZoneInfo
            requested = clock_module.date.fromisoformat(params['start'][:10])
            stamp = REAL_DATETIME.combine(requested,clock_module.time.min,ZoneInfo('America/New_York')).astimezone(clock_module.timezone.utc).isoformat().replace('+00:00','Z')
            body = result.json()
            groups = body['bars'].values() if isinstance(body['bars'],dict) else [body['bars']]
            for rows in groups:
                for row in rows:
                    row['t'] = stamp
            result = fixture.response(body)
        result.url = url
        return result
    requests.sessions.Session.request = request
    noaa_yearly_source = importlib.import_module('tradingagents.strategies.data_sources.noaa_yearly_source')
    noaa_yearly_source._yearly_http_get = lambda url,**kwargs: request(None,'GET',url,_yearly=True,**kwargs)
    def yahoo(*args,**kwargs):
        trace('sdk',endpoint='yfinance.download',tickers=args[0] if args else kwargs.get('tickers'))
        return fixture.yahoo(*args,**kwargs)
    yfinance.download = yahoo
    def earnings_dates(ticker,**kwargs):
        trace('sdk',endpoint='yfinance.Ticker.get_earnings_dates',ticker=ticker.ticker)
        day = REAL_DATETIME.fromisoformat(scenario()['now']) + clock_module.timedelta(days=30)
        return pd.DataFrame({'EPS Estimate':[1.0],'Reported EPS':[None],'Surprise(%)':[None]},index=pd.DatetimeIndex([day]))
    yfinance.Ticker.get_earnings_dates = earnings_dates
    def model_response(http_module,request):
        if request.url.host != 'api.openai.com' or request.url.path != '/v1/responses':
            trace('unexpected_httpx',url=str(request.url))
            raise AssertionError('unconfigured HTTPX endpoint')
        value = json.loads(request.content)
        text = _model_text(value)
        response = {'id':'resp_acceptance','model':value['model'],'status':'completed',
            'output':[{'id':'msg_acceptance','type':'message','role':'assistant','status':'completed',
                'content':[{'type':'output_text','text':text,'annotations':[]}]}]}
        body = 'data: '+json.dumps({'type':'response.completed','response':response})+'\n\n'
        return http_module.Response(200,headers={'content-type':'text/event-stream'},content=body.encode(),request=request)
    httpx.HTTPTransport.handle_request = lambda transport,request:model_response(httpx,request)
    # Current OpenAI SDKs can use the independently named HTTPX2 transport.
    # Substitute the same physical response boundary, never the SDK request.
    try:
        import httpx2
    except ImportError:
        pass
    else:
        httpx2.HTTPTransport.handle_request = lambda transport,request:model_response(httpx2,request)
    trace('bootstrap',network_denied=True,now=current['now'],
          ancestor_dotenv_present='EVENTEDGE_ACCEPTANCE_ANCESTOR_CANARY' in os.environ)
    if current.get('preflight_timeout') is not None:
        from tradingagents.strategies.orchestration import generation_manager
        generation_manager._GENERATION_TIMEOUT_S = current['preflight_timeout']
    if current.get('daily_timeout') is not None:
        from tradingagents.strategies.orchestration import runtime_recovery
        runtime_recovery.DAILY_TIMEOUT_SECONDS = current['daily_timeout']
