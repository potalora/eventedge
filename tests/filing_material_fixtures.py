"""Synthetic full native frames, using approved header identities and counts.

Document bodies and supplemental filenames are invented; no original-byte or
material-equivalence claims are made by these small offline fixtures.
"""
from tradingagents.strategies.data_sources.filing_evidence import frame_submission

OBSERVED = '2026-10-10T04:22:51+00:00'
ROWS = [
    ('0001193125-26-402806', '10-K', '2026-09-25', '0001512228',
     'NIOCORP DEVELOPMENTS LTD', '20260925160238', 310, '1512228',
     [(1, 'nb-20260630.htm')]),
    ('0000016732-26-000031', 'DEF 14A', '2026-10-07', '0000016732',
     "CAMPBELL'S Co", '20261007085538', 91, '16732',
     [(1, 'cpb-20261007.htm'), (81, 'cpb2026_courtesy-pdfa.pdf')]),
    ('0000818479-26-000278', '8-K', '2026-10-09', '0000818479',
     'DENTSPLY SIRONA Inc.', '20261009161618', 505, '818479',
     [(1, 'xray-20261008.htm')]),
]


def identity(index):
    accession, form, date, _, _, _, _, cik, _ = ROWS[index]
    return dict(accession=accession, form=form, filing_date=date,
        source_url=f'https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace("-", "")}/{accession}.txt')


def original(index):
    accession, form, date, cik, name, accepted, count, _, primary = ROWS[index]
    header = (f'<SEC-DOCUMENT>{accession}.txt : {date.replace("-", "")}\n'
        f'<SEC-HEADER>{accession}.hdr.sgml : {date.replace("-", "")}\n'
        f'<ACCEPTANCE-DATETIME>{accepted}\nACCESSION NUMBER: {accession}\n'
        f'CONFORMED SUBMISSION TYPE: {form}\nPUBLIC DOCUMENT COUNT: {count}\n'
        f'FILED AS OF DATE: {date.replace("-", "")}\nFILER:\nCOMPANY DATA:\n'
        f'COMPANY CONFORMED NAME: {name}\nCENTRAL INDEX KEY: {cik}\n</SEC-HEADER>\n')
    primary = dict(primary)
    parts = [header]
    for sequence in range(1, count + 1):
        filename = primary.get(sequence, f'fixture-{sequence}.jpg')
        kind = form if sequence in primary else 'GRAPHIC'
        body = '<PDF>\nsynthetic opaque PDF\n</PDF>' if filename.endswith('.pdf') else '<p>Synthetic full body.</p>'
        parts.append(f'<DOCUMENT>\n<TYPE>{kind}\n<SEQUENCE>{sequence}\n<FILENAME>{filename}\n'
                     f'<TEXT>\n{body}\n</TEXT>\n</DOCUMENT>\n')
    return (''.join(parts) + '</SEC-DOCUMENT>\n').encode()


def framed(index):
    row = identity(index)
    raw = original(index)
    return frame_submission(raw, expected_accession=row['accession'], expected_form=row['form'],
        expected_date=row['filing_date'], observed_at=OBSERVED, max_submission_bytes=512*1024**2), raw
