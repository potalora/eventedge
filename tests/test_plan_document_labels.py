"""Finite original labels are explicit and do not alter legacy document wire."""
from dataclasses import replace
import pytest
from tradingagents.strategies.plan_document_inputs import PlanDocumentInput,encode_document_inputs,document_input_content,document_input_metadata
from test_regulation_graphics import assets


def test_optin_labels_are_immediately_before_each_exact_original():
    docs=[PlanDocumentInput('image/png',a['data'],a['filename'],a['sha256'],presentation_policy='originals_label_v1')for a in assets()]
    wire=encode_document_inputs(docs);content=document_input_content(wire)
    assert len(content)==2*len(docs)
    for i,doc in enumerate(docs):
        assert content[2*i]=={'type':'input_text','text':f'Attached image {i+1} (one-based): {doc.filename}'}
        assert content[2*i+1]['type']=='input_image'and content[2*i+1]['detail']=='high'
    assert all(m['presentation_policy']=='originals_label_v1'for m in document_input_metadata(wire))


def test_default_wire_and_content_are_legacy_exact_and_mixed_unknown_fail():
    a=assets()[0];old=PlanDocumentInput('image/png',a['data'],a['filename'],a['sha256'])
    wire=encode_document_inputs([old]);assert set(wire[0])=={'media_type','filename','sha256','data_base64'}
    assert len(document_input_content(wire))==1
    with pytest.raises(ValueError):encode_document_inputs([old,replace(old,presentation_policy='originals_label_v1')])
    with pytest.raises(ValueError):replace(old,presentation_policy='arbitrary')
