"""Exact independent inventories resolve counts, never missing native bodies."""
import copy
import hashlib
import html
import json

import pytest

from test_filing_evidence import ACCESSION, OBSERVED, role, submission
from tradingagents.strategies.data_sources import filing_evidence as evidence

DIRECTORY=f'https://www.sec.gov/Archives/edgar/data/2065397/{ACCESSION.replace("-", "")}'
IDENTITY={'accession':ACCESSION,'form':'8-K','filing_date':'2026-09-30',
          'source_url':f'{DIRECTORY}/{ACCESSION}.txt'}
ARGUMENTS=dict(expected_accession=ACCESSION,expected_form='8-K',expected_date='2026-09-30',observed_at=OBSERVED)


def metadata_responses(raw, *, declared=2, documents=None):
    documents=documents or [('8-K',1,'main.htm')]
    comment=(f'<SEC-HEADER>{ACCESSION}.hdr.sgml : 20260930\n<ACCESSION-NUMBER>{ACCESSION}\n'
        f'<TYPE>8-K\n<PUBLIC-DOCUMENT-COUNT>{declared}\n<FILING-DATE>20260930\n'
        +role('FILER','0002065397','Sanitized Credit Fund LLC').replace('\r','\n')+'</SEC-HEADER>')
    pre=(f'<SEC-DOCUMENT>{ACCESSION}-index.html : 20260930\n<SEC-HEADER>{ACCESSION}.hdr.sgml : 20260930\n'
        f'ACCESSION NUMBER:\t{ACCESSION}\nCONFORMED SUBMISSION TYPE:\t8-K\nPUBLIC DOCUMENT COUNT:\t{declared}\n'
        'FILED AS OF DATE:\t20260930\nFILER:\n COMPANY DATA:\n'
        ' COMPANY CONFORMED NAME:\tSanitized Credit Fund LLC\n CENTRAL INDEX KEY:\t0002065397\n</SEC-HEADER>\n')
    for kind,seq,name in documents:
        pre+=f'<DOCUMENT>\n<TYPE>{kind}\n<SEQUENCE>{seq}\n<FILENAME>{name}\n<TEXT>\nDocument\n</DOCUMENT>\n'
    pre+='</SEC-DOCUMENT>'
    headers='<!--\n'+comment+'\n--><PRE>'+html.escape(pre)+'</PRE>'
    index=(f'<table><tr><td></td><td>Complete submission text file</td>'
        f'<td><a href="/Archives/edgar/data/2065397/{ACCESSION.replace("-", "")}/{ACCESSION}.txt">{ACCESSION}.txt</a></td>'
        f'<td></td><td>{len(raw)}</td></tr></table>')
    names=[f'{ACCESSION}-index.html',f'{ACCESSION}-index-headers.html',f'{ACCESSION}.txt']+[d[2] for d in documents]
    directory=json.dumps({'directory':{'name':DIRECTORY.removeprefix('https://www.sec.gov'),
        'item':[{'name':name,'size':'','type':'text.gif','last-modified':'2026-09-30 00:00:00'} for name in names]}})
    bodies={'index_headers':headers,'filing_index':index,'directory':directory}
    urls={'index_headers':f'{DIRECTORY}/{ACCESSION}-index-headers.html','filing_index':f'{DIRECTORY}/{ACCESSION}-index.html','directory':DIRECTORY+'/index.json'}
    return {key:{'body':body,'source_url':urls[key],'status_code':200,'complete':True,
        'size':len(body.encode()),'sha256':hashlib.sha256(body.encode()).hexdigest(),'observed_at':OBSERVED} for key,body in bodies.items()}


def proof_for(raw, **kwargs):
    from tradingagents.strategies.data_sources.filing_inventory_reconciliation import build_reconciliation
    return build_reconciliation(metadata_responses(raw,**kwargs),original_identity=IDENTITY,
        original_sha256=hashlib.sha256(raw).hexdigest(),original_size=len(raw),observed_at=OBSERVED)


def mismatched():return submission().replace(b'<PUBLIC-DOCUMENT-COUNT>1',b'<PUBLIC-DOCUMENT-COUNT>2')


def framed(raw,proof=None):
    return evidence.frame_submission(raw,**ARGUMENTS,max_submission_bytes=len(raw),inventory_reconciliation=proof)


def test_exact_corroboration_resolves_count_and_retains_proof_without_rewriting_original():
    raw=mismatched()
    with pytest.raises(evidence.EvidenceError,match='document_count_mismatch'):
        evidence.frame_submission(raw,**ARGUMENTS,max_submission_bytes=len(raw))
    result=framed(raw,proof_for(raw))
    selected=evidence.build_evidence_from_buffer(evidence.select_primary(result,raw),raw)
    assert selected['structural_status']=='complete'
    assert selected['submission_sha256']==hashlib.sha256(raw).hexdigest()
    proof=selected['inventory_reconciliation']
    assert proof['declared_document_count']==2 and proof['actual_document_count']==1
    assert proof['native_header_sha256']==selected['header_sha256']
    assert set(proof['metadata'])=={'index_headers','filing_index','directory'}
    from tradingagents.strategies.data_sources.filing_inventory_reconciliation import validate_reconciled_evidence
    validate_reconciled_evidence(selected,original={'identity':IDENTITY,'sha256':hashlib.sha256(raw).hexdigest(),'size':len(raw),'observed_at':OBSERVED})


@pytest.mark.parametrize('change,code',[
    (lambda r:r.replace(b'</TEXT>',b''),'incomplete_document_text'),
    (lambda r:r.replace(b'</DOCUMENT>',b''),'incomplete_document'),
    (lambda r:r.replace(b'<SEQUENCE>1',b'<SEQUENCE>0'),'invalid_document_sequence'),
    (lambda r:r.replace(b'<FILENAME>main.htm',b'<FILENAME>../main.htm'),'unsafe_document_filename'),
    (lambda r:r.replace(b'</FILER>',b''),'incomplete_role'),
])
def test_proof_never_exempts_other_native_checks(change,code):
    raw=change(mismatched())
    with pytest.raises(evidence.EvidenceError,match=code):framed(raw,proof_for(raw))


@pytest.mark.parametrize('kind',['role','tuple','directory_extra','directory_missing','size','receipt_hash','url','body_deleted','count'])
def test_tampered_metadata_fails_even_when_normalized_claims_could_agree(kind):
    raw=mismatched();metadata=metadata_responses(raw)
    if kind=='role':metadata['index_headers']['body']=metadata['index_headers']['body'].replace('0002065397','0002065398')
    elif kind=='tuple':metadata['index_headers']['body']=metadata['index_headers']['body'].replace('main.htm','other.htm')
    elif kind.startswith('directory_'):
        d=json.loads(metadata['directory']['body'])
        if kind.endswith('extra'):d['directory']['item'].append({'name':'extra.htm'})
        else:d['directory']['item'].pop()
        metadata['directory']['body']=json.dumps(d)
    elif kind=='size':metadata['filing_index']['body']=metadata['filing_index']['body'].replace(str(len(raw)),str(len(raw)+1))
    elif kind=='receipt_hash':metadata['directory']['sha256']='0'*64
    elif kind=='url':metadata['directory']['source_url']=metadata['directory']['source_url'].replace('/2065397/','/1/')
    elif kind=='body_deleted':del metadata['directory']['body']
    else:metadata['index_headers']['body']=metadata['index_headers']['body'].replace('COUNT&gt;2','COUNT&gt;3').replace('COUNT:\t2','COUNT:\t3')
    if kind not in ('receipt_hash','url','body_deleted'):
        for item in metadata.values():item.update(size=len(item['body'].encode()),sha256=hashlib.sha256(item['body'].encode()).hexdigest())
    from tradingagents.strategies.data_sources.filing_inventory_reconciliation import build_reconciliation
    with pytest.raises((ValueError,evidence.EvidenceError)):
        proof=build_reconciliation(metadata,original_identity=IDENTITY,original_sha256=hashlib.sha256(raw).hexdigest(),original_size=len(raw),observed_at=OBSERVED)
        framed(raw,proof)


def test_replay_rejects_mutated_inventory_identity_hash_and_fabricated_bound_flags():
    raw=mismatched();result=framed(raw,proof_for(raw))
    item=evidence.build_evidence_from_buffer(evidence.select_primary(result,raw),raw);item['source_url']=IDENTITY['source_url']
    from tradingagents.strategies.data_sources.filing_inventory_reconciliation import validate_reconciled_evidence
    for mutate in [lambda d:d['document_inventory'][0].update(filename='other.htm'),
        lambda d:d['roles'][0].update(cik='0002065398'),lambda d:d.update(submission_sha256='0'*64),
        lambda d:d['inventory_reconciliation'].update(actual_document_count=2),
        lambda d:d['inventory_reconciliation'].update(native_header_sha256='0'*64)]:
        bad=copy.deepcopy(item);mutate(bad)
        with pytest.raises(ValueError):validate_reconciled_evidence(bad)


def test_equal_or_malformed_count_never_uses_proof():
    for raw in [submission(),mismatched().replace(b'<PUBLIC-DOCUMENT-COUNT>2',b'<PUBLIC-DOCUMENT-COUNT>no')]:
        with pytest.raises((ValueError,evidence.EvidenceError)):framed(raw,proof_for(raw))


def test_directory_cik_must_appear_in_independent_full_role_inventory():
    raw=mismatched();metadata=metadata_responses(raw)
    for row in metadata.values():
        row['source_url']=row['source_url'].replace('/2065397/','/999/')
        row['body']=row['body'].replace('/2065397/','/999/')
        row.update(size=len(row['body'].encode()),sha256=hashlib.sha256(row['body'].encode()).hexdigest())
    from tradingagents.strategies.data_sources.filing_inventory_reconciliation import build_reconciliation
    with pytest.raises(ValueError):
        build_reconciliation(metadata,original_identity={**IDENTITY,'source_url':IDENTITY['source_url'].replace('/2065397/','/999/')},
            original_sha256=hashlib.sha256(raw).hexdigest(),original_size=len(raw),observed_at=OBSERVED)
