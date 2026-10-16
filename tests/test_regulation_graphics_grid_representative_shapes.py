"""Constructed EP000 physical geometry; not original-image OCR/accuracy proof.

The former V7 fixture is privately retained unchanged. This prospective compact
case preserves its grouped unnamed columns and merged law cells, not that wire.
"""
import json
import pytest
from tradingagents.strategies.data_sources import regulation_graphics_grid as g
from test_regulation_graphics_grid import provenance
from test_regulation_graphics_grid import assets,review_response
from test_regulation_graphics_compact_representative_shapes import empty,at,region


def merged_reference(request):
    value=empty()
    matrix=[['Commission Reference',None,'CFR Citation\n[17 CFR]'],
        ['Securities Act of 1933 (the “Securities Act”)\nand Investment Company Act of 1940 (the\n“Investment Company Act”)','Form N-1A','§ 239.15A and § 274.11A'],
        [None,'Form N-2','§ 239.14 and § 274.11a-1'],
        ['Investment Company Act and Securities\nExchange Act of 1934 (the “Exchange Act”)','Form N-CSR','§ 274.128 and § 249.331'],
        ['Investment Advisers Act of 1940 (the\n“Advisers Act”)','Rule 203A-3','§ 275.203A-3'],
        [None,'Rule 204-3','§ 275.204-3'],[None,'Rule 205-3','§ 275.205-3']]
    headers=[{'at':at(0,0),'source':None,'axis':'column','regions':[region(1,0,last_column=1)],'parents':[]},
        {'at':at(0,2),'source':None,'axis':'column','regions':[region(1,2)],'parents':[]}]
    for start,end in [(1,2),(3,3),(4,6)]:
        headers.append({'at':at(start,0),'source':None,'axis':'row',
            'regions':[region(start,1,last_row=end,last_column=2)],'parents':[]})
    value['tables']=[{'matrix':matrix,'spans':[
        {'anchor':{'row':0,'column':0},'through':{'row':0,'column':1}},
        {'anchor':{'row':1,'column':0},'through':{'row':2,'column':0}},
        {'anchor':{'row':4,'column':0},'through':{'row':6,'column':0}}],
        'headers':headers,'units':[]}]
    value['reading_order']=[{'table':0}]
    return value


def prepared():
    rows=assets(1);plan=g.prepare_execution_plan(rows);q=g.prepare_recognition(rows,plan,0)
    return rows,plan,q,merged_reference(q)


def test_unnamed_three_column_group_and_merged_laws_preserve_literals_and_review():
    rows,plan,q,value=prepared();rec=g.validate_recognition(json.dumps(value),q,provenance(q))
    assert rec['result']==value
    table=rec['result']['tables'][0]
    assert len(table['matrix'])==7 and len(table['matrix'][0])==3
    assert table['matrix'][0][1] is None and table['matrix'][2][0] is None
    assert table['matrix'][5][0] is None and table['matrix'][6][0] is None
    assert [h['at']for h in table['headers']if h['axis']=='column']==[at(0,0),at(0,2)]
    assert '\n' in table['matrix'][1][0]
    review=g.prepare_review(rows,plan,0,rec)
    result=review_response(review,rec)
    receipt=g.validate_review(json.dumps(result),review,rec,provenance(review))
    assert receipt['result']==result


@pytest.mark.parametrize('fault',['out_of_bounds_span','overlap','printed_covered_cell','bad_scope','bad_parent','wrong_anchor'])
def test_physical_spans_and_named_scope_bounds_cannot_be_invented(fault):
    _,_,q,value=prepared();table=value['tables'][0]
    if fault=='out_of_bounds_span':table['spans'][0]['through']['column']=3
    elif fault=='overlap':table['spans'].append(dict(table['spans'][1]))
    elif fault=='printed_covered_cell':table['matrix'][0][1]='Invented Form leaf'
    elif fault=='bad_scope':table['headers'][0]['regions'][0]['through']['row']=7
    elif fault=='bad_parent':table['headers'][0]['parents']=[2]
    elif fault=='wrong_anchor':table['headers'][0]['at']=at(0,1)
    with pytest.raises(g.RecognitionContentError):g.validate_recognition(json.dumps(value),q,provenance(q))


def test_pixel_review_remains_gate_for_internally_valid_but_wrong_header_association():
    rows,plan,q,value=prepared()
    # Both scopes are finite. Only the independent original-image reviewer can
    # establish that Commission Reference must not describe CFR citations.
    value['tables'][0]['headers'][0]['regions'][0]=region(1,2)
    rec=g.validate_recognition(json.dumps(value),q,provenance(q))
    review=g.prepare_review(rows,plan,0,rec);result=review_response(review,rec)
    result['checks']['typed_associations']=False
    result['errors']=[{'at':at(0,0),'issue':'Commission Reference is assigned to the wrong physical column.'}]
    with pytest.raises(g.ReviewRejected):g.validate_review(json.dumps(result),review,rec,provenance(review))
