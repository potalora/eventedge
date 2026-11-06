"""V3 whole-unit references preserve every long incorporation obligation."""
from copy import deepcopy
import hashlib
import pytest
from test_filing_evidence import ACCESSION, OBSERVED, role, submission
from test_filing_ownership_lineage import filing, FILER, SUBJECT, case, run
from tradingagents.strategies.data_sources import filing_ownership_lineage as lineage
from tradingagents.strategies.data_sources.filing_evidence import parse_submission, build_evidence_from_buffer


def with_exhibit(text, *, html=None, policy='verified_supported_documents_v3'):
    initial=filing(1,'2026-09-30')
    body=html or '<p>'+text+'</p>'
    raw=submission([(initial[0]['form'],'primary.xml',initial[2].decode()),('EX-99','contract.htm',body)],
        form=initial[0]['form'],roles=role('FILED-BY',FILER,'Source Filer')+role('SUBJECT-COMPANY',SUBJECT,'Subject'))
    raw=raw.replace(ACCESSION.encode(),initial[0]['accession'].encode())
    raw=raw.replace(b'<TYPE>',b'<ACCEPTANCE-DATETIME>20260930120000\r<TYPE>',1)
    parsed=parse_submission(raw,expected_accession=initial[0]['accession'],expected_form=initial[0]['form'],expected_date='2026-09-30',observed_at=OBSERVED)
    evidence=build_evidence_from_buffer(parsed,raw,supported_document_policy=policy)
    return evidence,parsed['documents'][0]['body'],raw


def long_text(size=70000,tail=' ISDA agreement incorporated by reference.'):
    return 'x'*(size-len(tail.encode()))+tail


def test_v3_long_unit_reference_is_full_source_bound_and_stays_unresolved_duty():
    evidence,body,raw=with_exhibit(long_text())
    assert evidence['issues']==[]
    facts=lineage.ownership_facts(evidence,body)
    assert facts['policy']==lineage.POLICY_V2
    reference=facts['incorporations'][0]
    assert reference['reference_kind']=='complete_selected_unit_v1'
    assert 'quote' not in reference
    assert reference['source_unit']['text_utf8_bytes']==70000
    assert reference['source_unit']['text_char_start']==0
    assert reference['source_unit']['text_char_end']==70000
    assert lineage.resolve_incorporation_reference(reference,evidence)==long_text()
    current=(evidence,facts,body)
    result=run(current,case(current))
    assert result['policy']==lineage.POLICY_V2
    assert result['status']=='acquisition_required'
    assert result['obligations'][0]['kind']=='resolve_incorporation'
    assert result['obligations'][0]['source_unit']==reference['source_unit']
    assert lineage.validate_ownership_facts(facts,evidence,body)==facts


@pytest.mark.parametrize('size',[65535,65536,65537])
def test_utf8_quote_boundary_keeps_inline_semantics_and_exact_character_range(size):
    tail=' incorporated by reference.'
    # A non-ASCII character makes byte and character offsets distinct.
    text='é'+'x'*(size-len(tail.encode())-2)+tail
    evidence,body,_=with_exhibit(text)
    facts=lineage.ownership_facts(evidence,body)
    reference=facts['incorporations'][0]
    if size<=65536:
        assert reference['quote']==text and 'reference_kind' not in reference
    else:
        assert reference['source_unit']['text_utf8_bytes']==size
        assert reference['source_unit']['text_char_end']==len(text)==size-1
    assert lineage.resolve_incorporation_reference(reference,evidence)==text


def test_legacy_long_triggered_unit_keeps_original_failure_and_long_nontrigger_is_unchanged():
    evidence,body,_=with_exhibit(long_text(),policy='verified_supported_documents_v2')
    assert evidence['issues'][0]['code']=='invalid_native_ownership_facts'
    with pytest.raises(lineage.OwnershipLineageError,match='byte_limit'):lineage.ownership_facts(evidence|{'issues':[],'structural_status':'complete'},body)
    evidence,body,_=with_exhibit('x'*70000)
    assert lineage.ownership_facts(evidence,body)['incorporations']==[]


@pytest.mark.parametrize('field,value',[('accession','0001193125-26-999999'),('filename','other.htm'),
    ('body_sha256','0'*64),('text_sha256','0'*64),('body_start',0),('body_end',1),
    ('text_utf8_bytes',1),('text_char_start',1),('text_char_end',12),('html_receipt_sha256','0'*64)])
def test_forged_or_partial_source_reference_fails(field,value):
    evidence,body,_=with_exhibit(long_text())
    reference=deepcopy(lineage.ownership_facts(evidence,body)['incorporations'][0])
    reference['source_unit'][field]=value
    with pytest.raises(lineage.OwnershipLineageError):lineage.resolve_incorporation_reference(reference,evidence)


def test_duplicate_or_missing_unit_and_resealed_target_set_are_rejected():
    evidence,body,_=with_exhibit(long_text(tail=' incorporated by reference 0001193125-26-000002.'))
    facts=lineage.ownership_facts(evidence,body);reference=facts['incorporations'][0]
    for units in [evidence['units'][:1],evidence['units']+[deepcopy(evidence['units'][1])]]:
        with pytest.raises(lineage.OwnershipLineageError):lineage.resolve_incorporation_reference(reference,evidence|{'units':units})
    forged=deepcopy(facts);forged['incorporations'][0]['target_accessions']=[]
    forged['facts_sha256']=lineage._digest({k:v for k,v in forged.items() if k!='facts_sha256'})
    current=(evidence,forged,body)
    assert run(current,case(current))['status']=='technical_failure'
    with pytest.raises(lineage.OwnershipLineageError):lineage.validate_ownership_facts(forged,evidence,body)


def test_inline_and_whole_unit_anchor_only_locators_survive_and_supply_exact_cik():
    from tradingagents.strategies.data_sources.filing_ownership_hydration import _reference_ciks
    target='0001193125-26-000002'
    url='https://www.sec.gov/Archives/edgar/data/42/'+target.replace('-','')+'/agreement.htm'
    for prefix in ['', 'x'*70000]:
        source_html='<p>'+prefix+' incorporated by reference <a href="'+url+'">the agreement</a>.</p>'
        evidence,body,_=with_exhibit('',html=source_html)
        facts=lineage.ownership_facts(evidence,body);reference=facts['incorporations'][0]
        assert reference['target_accessions']==[target]
        assert reference['target_documents']==[{'accession':target,'filename':'agreement.htm'}]
        obligation={'kind':'incorporated_submission','accession':target,'source_accession':evidence['accession'],**reference}
        assert _reference_ciks(obligation,{evidence['accession']:evidence})==['0000000042']


def test_v3_native_xml_inline_href_only_locator_is_resolved_from_exact_raw_leaf():
    from tradingagents.strategies.data_sources.filing_ownership_hydration import _reference_ciks
    target='0001193125-26-000002'
    url='https://www.sec.gov/Archives/edgar/data/42/'+target.replace('-','')+'/agreement.htm'
    initial=filing(1,'2026-09-30',text='Incorporated by reference <a href="'+url+'">the agreement</a>.')
    # Rebuild v3 native XML with no selected exhibits.
    raw=submission([(initial[0]['form'],'primary.xml',initial[2].decode())],form=initial[0]['form'],
        roles=role('FILED-BY',FILER,'Source Filer')+role('SUBJECT-COMPANY',SUBJECT,'Subject'))
    raw=raw.replace(ACCESSION.encode(),initial[0]['accession'].encode())
    parsed=parse_submission(raw,expected_accession=initial[0]['accession'],expected_form=initial[0]['form'],expected_date='2026-09-30',observed_at=OBSERVED)
    evidence=build_evidence_from_buffer(parsed,raw,supported_document_policy='verified_supported_documents_v3')
    facts=lineage.ownership_facts(evidence,parsed['documents'][0]['body'])
    reference=facts['incorporations'][0]
    assert url not in reference['quote']
    obligation={'kind':'incorporated_submission','accession':target,'source_accession':evidence['accession'],**reference}
    assert _reference_ciks(obligation,{evidence['accession']:evidence})==['0000000042']
    forged=deepcopy(evidence);forged['ownership_primary_body']=''
    with pytest.raises(lineage.OwnershipLineageError):_reference_ciks(obligation,{evidence['accession']:forged})


def foreign_submission(accession,text,exhibits=()):
    raw=submission([('8-K','main.htm','<p>'+text+'</p>'),*exhibits]).replace(ACCESSION.encode(),accession.encode())
    parsed=parse_submission(raw,expected_accession=accession,expected_form='8-K',expected_date='2026-09-30',observed_at=OBSERVED)
    return build_evidence_from_buffer(parsed,raw,supported_document_policy='verified_supported_documents_v3')


def test_agreement_heuristic_reads_words_after_inline_cap_and_keeps_required_exhibit_duty():
    target='0001193125-26-000002'
    evidence,body,_=with_exhibit(long_text(tail=' incorporated by reference '+target+' agreement.'))
    facts=lineage.ownership_facts(evidence,body);current=(evidence,facts,body);data=case(current)
    data[0][target]=foreign_submission(target,'Retained primary.',[('EX-99','required.htm','<p>Agreement terms.</p>')])
    result=run(current,data)
    assert result['status']=='acquisition_required'
    assert any(d['kind']=='incorporated_exhibit' and d['filename']=='required.htm' for d in result['obligations'])


def test_nested_long_incorporated_source_retains_full_reference_and_cycle_limits():
    target='0001193125-26-000002';nested='0001193125-26-000003'
    evidence,body,_=with_exhibit(long_text(tail=' incorporated by reference '+target+'.'))
    facts=lineage.ownership_facts(evidence,body);current=(evidence,facts,body);data=case(current)
    data[0][target]=foreign_submission(target,long_text(tail=' incorporated by reference '+nested+'.'))
    result=run(current,data)
    assert result['status']=='acquisition_required'
    obligation=next(d for d in result['obligations'] if d.get('accession')==nested)
    assert obligation['reference_kind']=='complete_selected_unit_v1'
    assert obligation['source_unit']['accession']==target
    assert obligation['source_unit']['text_utf8_bytes']==70000
    assert run(current,data,max_edges=1)['status']=='technical_failure'
    data[0][target]=foreign_submission(target,long_text(tail=' incorporated by reference '+evidence['accession']+'.'))
    assert run(current,data)['reason']=='cyclic_incorporation_reference'


def test_reference_resolver_keeps_inherited_deadline_and_source_text_changes_fail_saved_facts():
    evidence,body,_=with_exhibit(long_text())
    facts=lineage.ownership_facts(evidence,body);reference=facts['incorporations'][0]
    def expired():raise TimeoutError('inherited reference deadline')
    with pytest.raises(TimeoutError):lineage.resolve_incorporation_reference(reference,evidence,check=expired)
    changed=deepcopy(evidence);changed['units'][1]['text']+=' changed'
    with pytest.raises(lineage.OwnershipLineageError):lineage.validate_ownership_facts(facts,changed,body)
    missing=deepcopy(evidence);missing['document_inventory']=missing['document_inventory'][:1]
    with pytest.raises(lineage.OwnershipLineageError):lineage.resolve_incorporation_reference(reference,missing)


def test_hidden_structural_source_incorporation_is_not_removed_by_visibility_or_inline_tags():
    evidence,body,_=with_exhibit('',html='<div><p style="display:none">incorpo<span>rated</span> by reference ISDA agreement.</p><p>Visible.</p></div>')
    assert evidence['units'][1]['representation']=='structural_html'
    facts=lineage.ownership_facts(evidence,body)
    assert len(facts['incorporations'])==1
    assert 'incorporated by reference ISDA agreement.' in facts['incorporations'][0]['quote']
    current=(evidence,facts,body)
    result=run(current,case(current))
    assert result['status']=='acquisition_required'
    assert result['obligations'][0]['kind']=='resolve_incorporation'
