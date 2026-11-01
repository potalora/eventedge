"""External provider payloads shared by offline in-process and native fixtures.

No orchestration or pytest imports: transport children need only these external
response bodies, while the scheduled parent retains the real application flow.
"""
from collections import Counter
from datetime import date, datetime, timedelta, timezone
import io
import json
import math
import zipfile

import pandas as pd
import requests

SESSION = date(2026, 10, 2)


class InterruptedWorker(BaseException):
    """Simulate loss of the worker between durable cohort staging phases."""


class TransportFixture:
    """Representative provider payloads and controlled upstream failures."""

    def __init__(self):
        self.session = SESSION
        self.now = datetime(2026, 10, 2, 22, tzinfo=timezone.utc)
        self.calls = Counter()
        self.blocked_calls = Counter()
        self.request_trace = []
        self.model_calls = Counter()
        self.fault = None
        self.block_all = False
        self.interrupt_committee_at = None
        self.weather_disruption = False

    @staticmethod
    def response(payload, status=200, *, headers=None):
        response = requests.Response()
        response.status_code = status
        response._content = payload if isinstance(payload, bytes) else (payload if isinstance(payload, str) else json.dumps(payload)).encode()
        response._content_consumed = True
        response.headers.update(headers or {})
        return response

    @classmethod
    def finra_response(cls, payload, rows):
        """Construct only the external public-query response, with pagination proof."""
        assert payload['compareFilters'][0]['fieldName'] == 'settlementDate'
        assert payload['compareFilters'][0]['compareType'] == 'EQUAL'
        assert payload['domainFilters'][0]['fieldName'] == 'symbolCode'
        symbols = payload['domainFilters'][0]['values']
        day = payload['compareFilters'][0]['fieldValue']
        selected = [row for row in rows if row['symbolCode'] in symbols and row['settlementDate'] == day]
        page = selected[:payload['limit']]
        return cls.response(page, headers={'Content-Type': 'application/json',
            'Record-Total': str(len(selected)), 'Record-Offset': '0',
            'Record-Limit': str(payload['limit']), 'Total-Records-On-Page': str(len(page))})

    def http(self, method, url, **kwargs):
        if self.block_all:
            self.blocked_calls["http"] += 1
            raise AssertionError("accepted session attempted another provider call")
        self.calls[url.split("?")[0]] += 1
        params = kwargs.get("params") or {}
        self.request_trace.append((url, dict(params)))
        if url == 'https://api.finra.org/data/group/otcMarket/name/consolidatedShortInterest':
            assert method.lower() == 'post' and kwargs['stream'] is True and kwargs['allow_redirects'] is False
            return self.finra_response(kwargs['json'], self.finra_rows)
        if url.endswith('/CommitmentsofTraders/ReleaseSchedule/index.htm'):
            from test_cftc_freshness import calendar
            return self.response(calendar(self.session.year))
        if url.startswith('https://cftc.gov/files/dea/history/fut_disagg_txt_'):
            year = int(url.rsplit('_', 1)[1].removesuffix('.zip'))
            archive = io.BytesIO()
            with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as output:
                output.writestr('f_year.txt', self.cot(year).to_csv(index=False))
            return self.response(archive.getvalue())
        if "data.alpaca.markets" in url:
            requested = params["start"][:10]
            opening = 184 if requested == "2026-10-05" else 182
            def bars(ticker):
                values = [{"t": f"{requested}T04:00:00Z", "o": opening,
                           "h": opening + 4, "l": opening - 2, "c": opening + 2,
                           "v": 1_000_000, "n": 45_000, "vw": opening + 1}]
                if self.fault == "sip_incoherent" and ticker == "NVDA":
                    values[0]["l"] = opening + 1
                return values
            if url == "https://data.alpaca.markets/v2/stocks/bars":
                payload = {"bars": {ticker: bars(ticker) for ticker in params["symbols"].split(",")
                                    if not (self.fault == "sip_unsupported" and ticker == "NVDA")},
                           "next_page_token": None}
            else:
                ticker = url.split("/")[-2]
                if self.fault == "sip_unsupported" and ticker == "NVDA":
                    return self.response({}, 404)
                payload = {"symbol": ticker, "bars": bars(ticker), "next_page_token": None}
            return self.response(payload)
        if "finnhub.io" in url:
            if url.endswith("/calendar/earnings"):
                earnings = [{"symbol": "NVDA", "date": "2026-10-01", "year": 2026,
                    "quarter": 3, "epsActual": 1.25, "epsEstimate": 1.05,
                    "revenueActual": 46_000_000_000, "revenueEstimate": 44_000_000_000}]
                if self.session > SESSION:
                    # A distinct issuer/fiscal event keeps later staging and
                    # crash-boundary tests active without replaying NVDA's event.
                    earnings.append(dict(earnings[0], symbol="AAPL", date=str(self.session)))
                return self.response({"earningsCalendar": earnings})
            if url.endswith("/company-news"):
                symbol = params.get("symbol")
                if symbol == "NVDA":
                    return self.response([{"id": 901, "headline": "NVIDIA quarterly results exceed outlook",
                        "summary": "Revenue and earnings exceeded guidance; management raised its forecast.",
                        "source": "Reuters", "datetime": int(datetime(2026, 10, 2, 17, tzinfo=timezone.utc).timestamp()),
                        "url": "https://example.test/nvda-results", "category": "company"}])
                if symbol == "AAPL" and self.session > SESSION:
                    return self.response([{"id": 902, "headline": "Apple quarterly results exceed outlook",
                        "summary": "Apple earnings exceeded guidance; management raised its forecast.",
                        "source": "Reuters", "datetime": int(datetime.combine(self.session, datetime.min.time(), tzinfo=timezone.utc).replace(hour=17).timestamp()),
                        "url": "https://example.test/aapl-results", "category": "company"}])
                return self.response([])
            if url.endswith("/stock/peers"):
                return self.response([])
        if "efts.sec.gov" in url:
            call = self.calls[url]
            if self.fault in {"edgar_500_then_200", "edgar_429_then_200"} and call == 1:
                status = 500 if self.fault == "edgar_500_then_200" else 429
                return self.response({}, status, headers={"Retry-After": "0"})
            if self.fault == "edgar_timeout":
                raise requests.Timeout("offline exhausted upstream timeout")
            if self.fault == "edgar_partial" and params.get("forms") == "10-K":
                good = {"_source": {"form": "10-K", "file_date": "2026-10-01",
                    "adsh": "0001045810-26-000001", "display_names": ["NVIDIA Corporation (NVDA)"], "ciks": ["0001045810"]}}
                return self.response({"hits": {"hits": [good, {"_source": {"form": "10-K"}}]}})
            if self.fault == "edgar_malformed":
                return self.response({"hits": {"hits": [{"_source": {"form": "10-K"}}]}})
            return self.response({"hits": {"hits": [], "total":{"value":0,"relation":"eq"}}})
        if url.endswith("company_tickers.json"):
            symbols = ["AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA", "JPM"]
            return self.response({str(i): {"ticker": symbol, "title": symbol + " Corporation", "cik_str": i + 1000}
                                  for i, symbol in enumerate(symbols)})
        if "data.sec.gov/submissions" in url:
            # Synthetic native complete-recent inventory for the requested CIK.
            cik = int(url.rsplit("/", 1)[-1].removeprefix("CIK").removesuffix(".json"))
            response = self.response({"cik": cik, "filings": {"recent": {
                "form": [], "filingDate": [], "accessionNumber": [], "primaryDocument": [],
            }, "files": []}})
            response.url = url
            return response
        if "financialmodelingprep.com" in url:
            return self.response([])
        if "api.regulations.gov" in url:
            return self.response({"data": [], "meta":{"totalElements":0,"totalPages":1}})
        if "courtlistener.com" in url:
            return self.response({"results": [], "count": 0, "next": None})
        if "api.usaspending.gov" in url:
            return self.response({"results": [], "page_metadata": {"page":1,"hasNext": False}})
        if "ncei.noaa.gov/pub/data/ghcn/daily" in url:
            from tradingagents.strategies.data_sources.noaa_source import AG_STATES
            stations = {f"USC{i:08}": state for i, state in enumerate(AG_STATES, 1)}
            if url.endswith("ghcnd-stations.txt"):
                # Native GHCN catalog fixed columns: ID 1-11, state 39-40.
                return self.response("\n".join(f"{station:<11}{'':27}{state} OFFLINE STATION"
                                                for station, state in stations.items()))
            assert url.endswith("ghcnd-inventory.txt")
            # Native inventory: element 32-35, first year 37-40, last 42-45.
            return self.response("\n".join(f"{station:<11}{'':20}{dtype} 2020 2026"
                for station in stations for dtype in ("TMAX", "TMIN", "PRCP")))
        if "ncei.noaa.gov/access/services/data/v1" in url:
            assert params["dataset"] == "daily-summaries"
            assert params["units"] == "standard" and params["includeAttributes"] == "true"
            assert params["dataTypes"] == "TMAX,TMIN,PRCP"
            days = pd.date_range(params["startDate"], params["endDate"], freq="D")
            stations = params["stations"].split(",")
            assert 1 <= len(stations) <= 100
            return self.response([{"STATION": station, "DATE": str(day.date()),
                "TMAX": "85", "TMIN": "55", "PRCP": "0.12",
                "TMAX_ATTRIBUTES": ",,W", "TMIN_ATTRIBUTES": ",,W", "PRCP_ATTRIBUTES": ",,W"}
                for station in stations for day in days])
        if "quickstats.nass.usda.gov" in url:
            from tradingagents.strategies.data_sources.usda_source import condition_scope
            prior_sunday = self.session - timedelta(days=(self.session.weekday()+1)%7)
            scope = condition_scope(params["commodity_desc"], str(self.session))
            return self.response({"data":[{"commodity_desc":params["commodity_desc"],
                "year":params["year"], "state_alpha":state, "class_desc":crop_class,
                "week_ending":str(prior_sunday - timedelta(weeks=week)), "unit_desc":unit,"Value":value}
                for crop_class, states in scope["class_states"].items()
                for state in states if state in params["state_alpha"] for week in (1,0)
                for unit,value in [("PCT GOOD","50"),("PCT EXCELLENT","20"),("PCT FAIR","20"),("PCT POOR","8"),("PCT VERY POOR","2")]]})
        if "usdmdataservices.unl.edu" in url:
            assert params["statisticsType"] == 2
            observation = self.session - timedelta(days=(self.session.weekday()-1)%7)
            from tradingagents.strategies.data_sources.drought_monitor_source import STATE_FIPS
            by_fips = {fips: state for state, fips in STATE_FIPS.items()}
            return self.response([{"stateAbbreviation":by_fips[fips],"mapDate":observation.strftime("%Y%m%d"),
                "statisticFormatID":2,"none":0 if self.weather_disruption else 100,
                "d0":0,"d1":0,"d2":0,"d3":100 if self.weather_disruption else 0,"d4":0}
                for fips in params["aoi"].split(",")])
        raise AssertionError("unconfigured HTTP fixture endpoint: " + url)

    def yahoo(self, tickers, **kwargs):
        if self.block_all:
            self.blocked_calls["yahoo_sdk"] += 1
            raise AssertionError("accepted session attempted Yahoo SDK call")
        self.calls["yahoo_sdk"] += 1
        symbols = [tickers] if isinstance(tickers, str) else tickers
        # Provider end is exclusive; history has coherent varying observations.
        start = pd.Timestamp(kwargs.get("start", self.session - timedelta(days=90)))
        end = pd.Timestamp(kwargs.get("end", self.session + timedelta(days=1)))
        import exchange_calendars
        dates = pd.DatetimeIndex(list(exchange_calendars.get_calendar("XNYS").sessions_in_range(start, end - timedelta(days=1))))
        values = {}
        for symbol in symbols:
            base = 18 if symbol == "^VIX" else 182 if symbol == "NVDA" else 100
            closes = [base * (1 + .001 * math.sin(i * 1.7)) for i in range(len(dates))]
            for field in ("Open", "High", "Low", "Close", "Volume", "Dividends", "Stock Splits"):
                values[field, symbol] = (
                    [1_000_000] * len(dates) if field == "Volume" else
                    [0] * len(dates) if field in {"Dividends", "Stock Splits"} else
                    [value + (1 if field == "High" else -1 if field == "Low" else 0) for value in closes]
                )
        result = pd.DataFrame(values, index=dates)
        result.columns = pd.MultiIndex.from_tuples(result.columns)
        return result

    def fred(self, series_id, **kwargs):
        if self.block_all:
            self.blocked_calls["fred_sdk"] += 1
            raise AssertionError("accepted session attempted FRED SDK call")
        self.calls["fred_sdk"] += 1
        value = {"UNRATE": 4, "CPIAUCSL": 320, "PAYEMS": 160000, "ICSA": 210000,
                 "BAMLH0A0HYM2": 3.5, "BAMLC0A4CBBB": 1.2}.get(series_id, 2)
        frequency = "MS" if series_id in {"UNRATE", "CPIAUCSL", "PAYEMS", "FEDFUNDS"} else "W-THU" if series_id == "ICSA" else "B"
        return pd.Series(value, index=pd.date_range(kwargs["observation_start"], kwargs["observation_end"], freq=frequency))

    def cot(self, year, **kwargs):
        if self.block_all:
            self.blocked_calls["cftc_sdk"] += 1
            raise AssertionError("accepted session attempted CFTC SDK call")
        from tradingagents.strategies.data_sources.cftc_source import COMMODITY_CODES
        self.calls["cftc_sdk"] += 1
        # A real annual archive, with current latest report central in a full
        # rolling window; prior-year rows are required near New Year.
        days = pd.date_range(f"{year}-01-01", f"{year}-12-31", freq="W-TUE")
        latest = self.session - timedelta(days=(self.session.weekday()-1)%7)
        return pd.DataFrame([
            {"Market_and_Exchange_Names":name, "Report_Date_as_YYYY-MM-DD":str(day.date()),
             "M_Money_Positions_Long_All":1000 + (19 if day.date()==latest else day.isocalendar().week),
             "M_Money_Positions_Short_All":1000}
            for name in COMMODITY_CODES.values() for day in days])

    def model(self, client, *, system, prompt, **kwargs):
        if "portfolio manager" in system:
            self.model_calls["committee"] += 1
            if self.model_calls["committee"] == self.interrupt_committee_at:
                raise InterruptedWorker("worker stopped after earlier cohort staging")
            # The deterministic fixture recommends only a thesis actually
            # admitted to this decision; repeated events may already be used.
            signal_section = prompt.split("Signals (all ", 1)[1].split("Current positions", 1)[0]
            admitted = [json.loads(line[line.index("{"):]) for line in signal_section.splitlines()
                        if line.startswith("  ") and "{" in line]
            if not any(row.get("ticker") == "NVDA" and row.get("direction") == "long"
                       and row.get("strategy") == "earnings_call" for row in admitted):
                return "[]"
            return json.dumps([{"ticker": "NVDA", "direction": "long", "position_size_pct": .05,
                                "confidence": .9, "rationale": "Material quarterly earnings and outlook surprise",
                                "contributing_strategies": ["earnings_call"], "regime_alignment": "aligned"}])
        self.model_calls["enrichment"] += 1
        return json.dumps({"direction": "long", "conviction": .9,
                           "rationale": "Quarterly earnings exceed forecast with raised guidance",
                           "evidence_claim": "Quarterly earnings exceed forecast with raised guidance"})


