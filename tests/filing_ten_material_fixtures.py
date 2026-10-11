"""Small synthetic bodies for the seven newly approved exact native identities."""
from filing_material_fixtures import original as old_original, identity as old_identity, OBSERVED
from tradingagents.strategies.data_sources.filing_evidence import frame_submission

TEN_POLICY='retained_ten_material_gaps_v1'
ROWS=[
    ('0001308179-26-000423','DEF 14A','2026-10-06','0002023554','Sandisk Corp','20261006161534',162,[(1,'sndk-20261005.htm'),(2,'sndk_courtesy-pdf.pdf')]),
    ('0001308179-26-000420','DEF 14A','2026-10-06','0001633978','Lumentum Holdings Inc.','20261006160546',54,[(1,'lite2026-def14a.htm'),(2,'lite_courtesy-pdf.pdf')]),
    ('0001140361-26-037937','DEF 14A','2026-09-29','0001383312','BROADRIDGE FINANCIAL SOLUTIONS, INC.','20260929090033',118,[(1,'ny20075597x1_def14a.htm'),(2,'ny20075597x1_def14a.pdf')]),
    ('0001116132-26-000024','DEF 14A','2026-09-25','0001116132','TAPESTRY, INC.','20260925160153',81,[(1,'tpr-20260924.htm'),(2,'tpr2026_courtesy-pdf.pdf')]),
    ('0001482541-26-000056','8-K','2026-10-02','0001482541','BNB Standard Corp','20261002160527',58,[(1,'bnc-20260929.htm')]),
    ('0001628280-26-063740','8-K','2026-09-28','0001736730','Camp4 Therapeutics Corp','20260928164756',69,[(1,'camp-20260928.htm')]),
    ('0001193125-26-402928','8-K','2026-09-25','0002128626','Vylor Inc.','20260925164217',33,[(1,'ck0002128626-20260924.htm')]),
]


def identity(index):
    if index<3:return old_identity(index)
    accession,form,date,cik,*_=ROWS[index-3]
    return {'accession':accession,'form':form,'filing_date':date,
        'source_url':f'https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace("-", "")}/{accession}.txt'}


def original(index):
    if index<3:return old_original(index)
    accession,form,date,cik,name,accepted,count,primaries=ROWS[index-3];primary=dict(primaries)
    parts=[f'<SEC-DOCUMENT>{accession}.txt : {date.replace("-", "")}\n<SEC-HEADER>{accession}.hdr.sgml : {date.replace("-", "")}\n'
        f'<ACCEPTANCE-DATETIME>{accepted}\nACCESSION NUMBER: {accession}\nCONFORMED SUBMISSION TYPE: {form}\n'
        f'PUBLIC DOCUMENT COUNT: {count}\nFILED AS OF DATE: {date.replace("-", "")}\nFILER:\nCOMPANY DATA:\n'
        f'COMPANY CONFORMED NAME: {name}\nCENTRAL INDEX KEY: {cik}\n</SEC-HEADER>\n']
    for seq in range(1,count+1):
        name=primary.get(seq,f'fixture-{seq}.jpg');kind=form if seq in primary else 'GRAPHIC'
        body='<PDF>\nsynthetic opaque PDF\n</PDF>' if name.endswith('.pdf') else '<p>Synthetic full body.</p>'
        parts.append(f'<DOCUMENT>\n<TYPE>{kind}\n<SEQUENCE>{seq}\n<FILENAME>{name}\n<TEXT>\n{body}\n</TEXT>\n</DOCUMENT>\n')
    return (''.join(parts)+'</SEC-DOCUMENT>\n').encode()


def framed(index):
    row=identity(index);raw=original(index)
    return frame_submission(raw,expected_accession=row['accession'],expected_form=row['form'],expected_date=row['filing_date'],observed_at=OBSERVED,max_submission_bytes=512*1024**2),raw
