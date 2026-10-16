"""Exact same-original native locators never waive an external incorporation."""
from copy import deepcopy
import re
from xml.sax.saxutils import escape

import pytest

from test_filing_evidence import ACCESSION, OBSERVED, role, submission
from test_filing_ownership_lineage import filing, FILER, SUBJECT, case, run
from tradingagents.strategies.data_sources import filing_ownership_lineage as lineage
from tradingagents.strategies.data_sources.filing_evidence import parse_submission, build_evidence_from_buffer


def source(*, item4='', item5='', number_of_shares='', item6='', exhibits=(), amendment=None,
           omit_cover=None, duplicate_item=None, policy='verified_supported_documents_v3'):
    initial=filing(1,'2026-09-30',amendment=amendment)
    xml=initial[2].decode()
    rows={'soleVotingPower':'12','sharedVotingPower':'0','soleDispositivePower':'12',
        'sharedDispositivePower':'0','aggregateAmountOwned':'12','percentOfClass':'5.5'}
    if omit_cover:rows.pop(omit_cover)
    xml=xml.replace('</reportingPersonInfo>', ''.join(f'<{k}>{v}</{k}>' for k,v in rows.items())+'</reportingPersonInfo>')
    items=''
    for index,values in [(4,{'transactionPurpose':item4}),(5,{'percentageOfClassSecurities':item5,'numberOfShares':number_of_shares}),(6,{'contractDescription':item6})]:
        element=f'<item{index}>'+''.join(f'<{k}>{escape(v)}</{k}>' for k,v in values.items() if v)+f'</item{index}>'
        items+=element+(element if duplicate_item==index else '')
    xml=re.sub(r'<items1To7>.*?</items1To7>','<items1To7>'+items+'</items1To7>',xml,flags=re.S)
    raw=submission([(initial[0]['form'],'primary.xml',xml)]+list(exhibits),form=initial[0]['form'],
        roles=role('FILED-BY',FILER,'Source Filer')+role('SUBJECT-COMPANY',SUBJECT,'Subject'))
    raw=raw.replace(ACCESSION.encode(),initial[0]['accession'].encode())
    parsed=parse_submission(raw,expected_accession=initial[0]['accession'],expected_form=initial[0]['form'],expected_date='2026-09-30',observed_at=OBSERVED)
    evidence=build_evidence_from_buffer(parsed,raw,supported_document_policy=policy)
    assert evidence['issues']==[],evidence['issues']
    primary=parsed['documents'][0]['body']
    return evidence,lineage.ownership_facts(evidence,primary),primary


def item_reference(number=6,destination=4):
    return f'The information set forth in Item {number} of this Schedule 13D is incorporated by reference into this Item {destination}.'


def exhibit_reference(number='99.1'):
    return f'The joint filing agreement, a copy of which is attached hereto as Exhibit {number} and is incorporated herein by reference.'


def targets(current):
    return [(r['target_accessions'],r['target_documents']) for r in current[1]['incorporations']]


def test_exact_item_binds_current_primary_and_full_raw_replay():
    current=source(item4=item_reference(),item6='A complete contract description.')
    acc=current[0]['accession']
    assert targets(current)==[([acc],[{'accession':acc,'filename':'primary.xml'}])]
    data=case(current);result=run(current,data)
    assert result['status']=='complete' and result['obligations']==[]
    assert lineage.validate_ownership_lineage(result,current[0],data[1],data[0],data[2],data[3],data[4])==result


def test_exact_attached_exhibit_and_item_clause_bind_both_selected_documents():
    text=exhibit_reference()+' '+item_reference(4,6)
    current=source(item4='The subscription agreement.',item6=text,
        exhibits=[('EX-99.1','joint.htm','<p>The exact joint filing agreement.</p>')])
    acc=current[0]['accession']
    assert targets(current)==[([acc],[{'accession':acc,'filename':'joint.htm'},{'accession':acc,'filename':'primary.xml'}])]
    assert run(current,case(current))['status']=='complete'


@pytest.mark.parametrize('rows',[ '(7) through (10)', '(11) and (13)' ])
def test_cover_rows_bind_only_actual_fields_of_current_amendment(rows):
    text=f'The information set forth in rows {rows} of the cover page of this Amendment No. 1 is incorporated by reference into this Item 5.'
    current=source(item5=text,amendment=1)
    assert targets(current)==[([current[0]['accession']],[{'accession':current[0]['accession'],'filename':'primary.xml'}])]


@pytest.mark.parametrize('mutation',['unknown_external','missing_item','duplicate_item','wrong_destination','wrong_schedule','hidden_link','wrong_amendment','legacy'])
def test_missing_ambiguous_or_mixed_reference_remains_unresolved(mutation):
    kwargs={'item4':item_reference(),'item6':'The local contract.'}
    if mutation=='unknown_external':kwargs['item4']+=' The ISDA definitions are incorporated by reference.'
    if mutation=='missing_item':kwargs['item6']=''
    if mutation=='duplicate_item':kwargs['duplicate_item']=6
    if mutation=='wrong_destination':kwargs['item4']=item_reference(destination=5)
    if mutation=='wrong_schedule':kwargs['item4']=item_reference().replace('this Schedule','another Schedule')
    if mutation=='hidden_link':kwargs['item4']=item_reference().replace('information','<a href="https://external.example/agreement">information</a>')
    if mutation=='wrong_amendment':kwargs['item4']=item_reference().replace('Schedule 13D','Amendment No. 2');kwargs['amendment']=1
    if mutation=='legacy':kwargs['policy']='verified_supported_documents_v2'
    current=source(**kwargs)
    assert targets(current)==[([],[])]
    assert any(o['kind']=='resolve_incorporation' for o in run(current,case(current))['obligations'])


@pytest.mark.parametrize('exhibits',[[],[('EX-99.2','other.htm','<p>Other.</p>')],
    [('EX-99.1','a.htm','<p>A.</p>'),('EX-99.1','b.htm','<p>B.</p>')]])
def test_attached_exhibit_requires_unique_exact_native_type(exhibits):
    current=source(item6=exhibit_reference(),exhibits=exhibits)
    assert targets(current)==[([],[])]


def test_missing_cover_row_cannot_be_proved_by_other_fields():
    current=source(item5='The information set forth in rows (7) through (10) of the cover page of this Amendment No. 1 is incorporated by reference into this Item 5.',amendment=1,omit_cover='sharedDispositivePower')
    assert targets(current)==[([],[])]


def test_attached_exhibit_keeps_its_own_external_incorporation_duty():
    current=source(item6=exhibit_reference(),exhibits=[('EX-99.1','joint.htm','<p>HKIAC rules are incorporated by reference.</p>')])
    result=run(current,case(current))
    assert result['status']=='acquisition_required'
    assert [o['source_path'] for o in result['obligations'] if o['kind']=='resolve_incorporation']==['unit:joint.htm']


def test_resealed_forged_local_target_rejected_by_recomputation():
    current=source(item4=item_reference(),item6='A contract.')
    evidence,facts,body=current;facts=deepcopy(facts)
    facts['incorporations'][0]['target_documents'][0]['filename']='invented.htm'
    facts['facts_sha256']=lineage._digest({k:v for k,v in facts.items() if k!='facts_sha256'})
    forged=evidence,facts,body
    assert run(forged,case(forged))['status']=='technical_failure'
    with pytest.raises(lineage.OwnershipLineageError):lineage.validate_ownership_facts(facts,evidence,body)
