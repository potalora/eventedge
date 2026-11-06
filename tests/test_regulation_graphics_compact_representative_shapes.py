"""Constructed actual-format cases; demo pixels are never native accuracy proof.

Layouts come from inspected government originals. Expected text stays in tests,
never recognition prompts. No fixture claims to be a complete OCR transcription.
"""
from copy import deepcopy
import json
import pytest
from tradingagents.strategies.data_sources import regulation_graphics_grid as g
from test_regulation_graphics_grid import provenance
from test_regulation_graphics_grid import assets


def empty():
    return {'status':'complete','context_closure':'resolved','text':[],'tables':[],
        'notes':[],'continuations':[],'visuals':[],'reading_order':[],'unresolved':[]}


def at(row,column,table=0):return {'table':table,'row':row,'column':column}
def region(row,column,last_row='last',last_column=None):
    return {'from':{'row':row,'column':column},
        'through':{'row':last_row,'column':column if last_column is None else last_column}}
def source(quote,anchor,image_position=2):
    return {'image_position':image_position,'quote':quote,
        'anchors':[{'at':anchor,'marker_text':None}],
        'association':'Visible aligned table continuation and matching local printed cell establish this association.'}
def visual(kind,description,literals=(),relations=()):
    return {'kind':kind,'description':description,'precision':'qualitative',
        'literals':list(literals),'relations':list(relations)}
def relation(kind,*destinations):return {'kind':kind,'to':list(destinations)}


def reverse_notes(request):
    """EP002 as target: local note printing, EP001 consumers explicitly foreign."""
    value=empty()
    for marker,body,quote in [
        ('e','Form ADV defines separately managed accounts as accounts of clients other than those listed in Part 1A,\nItem 5.D.(3), lines (d) through (f).', '3. Separately managed accounts:ᵉ'),
        ('f','As reported on Form ADV in Part 1A, Item 5.D., line (b).','--- High net worth individualsᶠ'),
        ('g','As reported on Form ADV in Part 1A, Item 5.D., line(a).','--- Other individualsᵍ')]:
        glyph={'e':'ᵉ','f':'ᶠ','g':'ᵍ'}[marker]
        value['notes'].append({'marker':marker,'body':body,'image_position':1,'anchors':[],'scopes':[],
            'consumer_sources':[{'image_position':2,'quote':quote,'marker_text':glyph,
                'association':'The raised table marker matches this local baseline note label.'}]})
        value['reading_order'].append({'note':len(value['notes'])-1})
    return value


def inherited_headers(request):
    """Headerless continuation: no fabricated target header row or leaf labels."""
    value=empty();matrix=[['Internal cost','$470.5'],['Total','$941.0']]
    value['tables']=[{'matrix':matrix,'spans':[],
        'headers':[{'at':None,'source':source('Burden category',at(0,0)),
                    'axis':'column','regions':[region(0,0)],'parents':[]},
                   {'at':None,'source':source('Internal Time Cost',at(0,1)),
                    'axis':'column','regions':[region(0,1)],'parents':[]}],
        'units':[{'at':at(0,1),'quote':'$','regions':[region(0,1)],'consumers':[]}]}]
    value['reading_order']=[{'table':0}]
    return value


def split_body(request):
    """EP088/089-type body split, distinct from merely inheriting headings."""
    value=empty();value['tables']=[{'matrix':[['Wage rate','Time cost'],['$470.5 per hour','$470.5']],
        'spans':[],'headers':[{'at':at(0,i),'source':None,'axis':'column',
            'regions':[region(1,i)],'parents':[]}for i in range(2)],
        'units':[{'at':at(1,i),'quote':'$','regions':[region(1,i)],'consumers':[]}for i in range(2)]}]
    value['continuations']=[{'cell':at(1,0),'before':[],
        'after':[source('(blended current rate for lawyers ($774), bookkeeping, accounting and auditing clerks ($167))',at(1,0),image_position=3)]},
        {'cell':at(1,1),'before':[],
        'after':[source('(Internal Annual Hour Burden of 1 hour x Wage rate of $470.5)',at(1,1),image_position=3)]}]
    value['reading_order']=[{'table':0}]
    return value


def dual_axes(request):
    """EP072-type two scale associations, separate printed equal unit occurrences."""
    value=empty();value['text']=['$ billion','$ billion','5,000','600','1/1/2026',
        'market capitalization (left axis)','trading volume (right axis)',
        'Figure 1: Market capitalization and 24-hour trading volume of global crypto markets. Data\nsource: CoinGecko (April 2026).']
    value['visuals']=[
        visual('axis','Left vertical scale'),visual('axis','Right vertical scale'),
        visual('axis','Unlabelled horizontal date scale'),
        visual('unit','Printed unit beside the left scale',[{'text':0}],[relation('unit',{'visual':0})]),
        visual('unit','Printed unit beside the right scale',[{'text':1}],[relation('unit',{'visual':1})]),
        visual('series','Dark curve with broad late peaks and a dark legend key',[{'text':5}],
            [relation('axis',{'visual':0},{'visual':2}),relation('unit',{'visual':3})]),
        visual('series','Lighter jagged curve with narrow spikes and a light legend key',[{'text':6}],
            [relation('axis',{'visual':1},{'visual':2}),relation('unit',{'visual':4})]),
        visual('caption','Caption directly below the chart',[{'text':7}],
            [relation('caption',{'visual':5},{'visual':6})]),
        visual('tick','Upper printed tick on left scale',[{'text':2}],[relation('axis',{'visual':0})]),
        visual('tick','Upper printed tick on right scale',[{'text':3}],[relation('axis',{'visual':1})]),
        visual('tick','Printed date at lower scale',[{'text':4}],[relation('axis',{'visual':2})])]
    value['reading_order']=[{'text':i}for i in range(len(value['text']))]
    return value


def mixed_form(request):
    """Constructed EP025/112-type expense grid, prose, fields and empty boxes."""
    value=empty();value['text']=['Shareholder Transaction Expenses',
        'Sales Load (as a percentage of offering price)','%',
        'Instructions.\nGeneral Instructions','(a) funds (e.g. cash or bank accounts)?',
        'Yes','No','Total Number of Clients']
    value['tables']=[{'matrix':[['Example','1 year','3 years','5 years','10 years'],
        ['You would pay the following expenses\non a $10,000 investment, assuming a\n5% annual return:','$','$','$','$']],
        'spans':[],'headers':[{'at':at(0,c),'source':None,'axis':'column',
            'regions':[region(1,c)],'parents':[]}for c in range(1,5)],'units':[]}]
    value['visuals']=[visual('form','Unfilled horizontal amount line',[at(1,c)],
        [relation('field',at(1,c))])for c in range(1,5)]
    value['visuals'] += [
        visual('form','Unfilled horizontal percentage line',[{'text':1}],
            [relation('field',{'text':1}),relation('unit',{'text':2})]),
        visual('form','Empty square in the Yes column',[{'text':4},{'text':5}],
            [relation('field',{'text':4},{'text':5})]),
        visual('form','Empty square in the No column',[{'text':4},{'text':6}],
            [relation('field',{'text':4},{'text':6})]),
        visual('form','Unfilled count line',[{'text':7}],[relation('field',{'text':7})]),
        visual('caption','Instructions printed after the expense grid',[{'text':3}],
            [relation('caption',{'table':0})])]
    value['reading_order']=[{'text':0},{'text':1},{'text':2},{'table':0},
        *[{'text':i}for i in range(3,8)]]
    return value


def split_prefix(request):
    """EP089-type target retains local blank stub and foreign prefix separately."""
    value=empty()
    value['tables']=[{'matrix':[[None,'Wage Rate','Internal Time Costs'],
        [None,'(blended current rate for lawyers ($774), bookkeeping, accounting and auditing clerks ($167))',
         '(Internal Annual Hour Burden of 1 hour x Wage rate of $470.5)']],
        'spans':[],'headers':[{'at':at(0,c),'source':None,'axis':'column',
            'regions':[region(1,c)],'parents':[]}for c in (1,2)],'units':[]}]
    value['continuations']=[
        {'cell':at(1,0),'before':[source('Inadvertent Custody',at(1,1))],'after':[]},
        {'cell':at(1,1),'before':[source('$470.5 per hour',at(1,1))],'after':[]},
        {'cell':at(1,2),'before':[source('$470.5',at(1,2))],'after':[]}]
    value['reading_order']=[{'table':0}]
    return value


def repeated_markers(request):
    """The same printed note letter can identify different local table notes."""
    value=empty()
    for table_index,number in enumerate(('1','2')):
        value['tables'].append({'matrix':[['Printed headingᵃ','Count'],['Category',number]],
            'spans':[],'headers':[],'units':[]})
        value['notes'].append({'marker':'a','body':'Distinct local note '+number+'.',
            'image_position':1,'anchors':[{'at':at(0,0,table_index),'marker_text':'ᵃ'}],
            'scopes':[{'table':table_index,'region':region(1,0,last_column='last')}],
            'consumer_sources':[]})
        value['reading_order'] += [{'table':table_index},{'note':table_index}]
    return value


CASES={'reverse_notes':reverse_notes,'inherited_headers':inherited_headers,'split_body':split_body,
    'dual_axes':dual_axes,'mixed_form':mixed_form,'split_prefix':split_prefix,'repeated_markers':repeated_markers}


def representative_response(request,case):return CASES[case](request)


def representative_review_response(recognition):
    value=recognition['result']
    return {'complete':True,'context_resolved':True,
        'checked_text':[True]*len(value['text']),
        'checked_tables':[{'cells':[[True]*len(row)for row in table['matrix']],
            'spans':[True]*len(table['spans']),'headers':[True]*len(table['headers']),
            'units':[True]*len(table['units'])}for table in value['tables']],
        'checked_notes':[True]*len(value['notes']),
        'checked_continuations':[{'before':[True]*len(part['before']),'after':[True]*len(part['after'])}for part in value['continuations']],
        'checked_visuals':[{'object':True,'relations':[True]*len(v['relations'])}for v in value['visuals']],
        'checked_reading_order':[True]*len(value['reading_order']),
        'checks':{key:True for key in g.REVIEW_CHECKS},'errors':[]}


def prepare_case(case):
    rows=assets(3)
    start={'reverse_notes':1,'inherited_headers':99,'split_body':87,'dual_axes':71,'mixed_form':24,'split_prefix':88,'repeated_markers':24}[case]
    prefix='EP05OC26.' if case=='mixed_form' else 'EP06OC26.'
    rows=[dict(row,id=prefix+str(start+i).zfill(3),filename=prefix+str(start+i).zfill(3)+'.png')for i,row in enumerate(rows)]
    plan=g.prepare_execution_plan(rows);request=g.prepare_recognition(rows,plan,1)
    value=representative_response(request,case)
    return rows,plan,request,value


@pytest.mark.parametrize('case',list(CASES))
def test_all_representative_formats_validate_and_receive_full_original_review(case):
    rows,plan,request,value=prepare_case(case)
    recognition=g.validate_recognition(json.dumps(value),request,provenance(request))
    assert recognition['result']==value
    review=g.prepare_review(rows,plan,1,recognition)
    result=representative_review_response(recognition)
    receipt=g.validate_review(json.dumps(result),review,recognition,provenance(review))
    assert receipt['result']==result
    # The fixture's expected content is output only, never a caller-supplied key.
    assert 'expected_answer' not in request.user and 'ground_truth' not in request.user


@pytest.mark.parametrize('fault',['missing_consumer','target_as_foreign','bad_marker','foreign_body_without_local_anchor'])
def test_note_only_target_cannot_lose_or_forge_foreign_consumer(fault):
    _,_,request,value=prepare_case('reverse_notes');note=value['notes'][0]
    if fault=='missing_consumer':note['consumer_sources']=[]
    elif fault=='target_as_foreign':note['consumer_sources'][0]['image_position']=1
    elif fault=='bad_marker':note['consumer_sources'][0]['marker_text']='absent'
    elif fault=='foreign_body_without_local_anchor':note['image_position']=2
    with pytest.raises(g.RecognitionContentError):g.validate_recognition(json.dumps(value),request,provenance(request))


@pytest.mark.parametrize('case,fault',[('inherited_headers','foreign_target'),('inherited_headers','missing_anchor'),
    ('split_body','bad_position'),('split_body','missing_anchor'),('split_body','duplicate_cell'),
    ('dual_axes','dangling_axis'),('mixed_form','missing_order'),('mixed_form','duplicate_order')])
def test_association_identity_order_and_geometry_cannot_be_silently_defaulted(case,fault):
    _,_,request,value=prepare_case(case)
    if fault=='foreign_target':value['tables'][0]['headers'][0]['source']['image_position']=1
    elif fault=='missing_anchor':
        source_value=(value['tables'][0]['headers'][0]['source']if case=='inherited_headers'else value['continuations'][0]['after'][0])
        source_value['anchors']=[]
    elif fault=='bad_position':value['continuations'][0]['after'][0]['image_position']=99
    elif fault=='duplicate_cell':value['continuations'].append(deepcopy(value['continuations'][0]))
    elif fault=='dangling_axis':value['visuals'][5]['relations'][0]['to']=[{'visual':99}]
    elif fault=='missing_order':value['reading_order'].pop()
    elif fault=='duplicate_order':value['reading_order'].append(dict(value['reading_order'][0]))
    with pytest.raises(g.RecognitionContentError):g.validate_recognition(json.dumps(value),request,provenance(request))


@pytest.mark.parametrize('case,mask',[('reverse_notes','checked_notes'),('inherited_headers','checked_tables'),
    ('split_body','checked_continuations'),('dual_axes','checked_visuals'),('mixed_form','checked_reading_order')])
def test_independent_review_must_check_every_format_position_and_whole_original(case,mask):
    rows,plan,request,value=prepare_case(case)
    recognition=g.validate_recognition(json.dumps(value),request,provenance(request))
    review=g.prepare_review(rows,plan,1,recognition);result=representative_review_response(recognition)
    result[mask]=[]
    with pytest.raises(g.ReviewMalformed):g.validate_review(json.dumps(result),review,recognition,provenance(review))
    result=representative_review_response(recognition);result['checks']['whole_original_omissions']=False
    result['errors']=[{'at':None,'issue':'Constructed reviewer finds omitted original material.'}]
    with pytest.raises(g.ReviewRejected):g.validate_review(json.dumps(result),review,recognition,provenance(review))


def representative_bundle(rows,plan,index,case):
    from test_regulation_graphics_grid import bundle
    return bundle(rows,plan,index,response_factory=lambda request:representative_response(request,case))


@pytest.mark.parametrize('case',list(CASES))
def test_full_target_replay_preserves_exact_content_and_runtime_original_bindings(case):
    rows,plan,request,value=prepare_case(case)
    bundle=representative_bundle(rows,plan,1,case)
    material=g.replay_target(rows,plan,1,bundle)[0]
    assert material['id']==rows[1]['id'] and material['sha256']==rows[1]['sha256']
    assert material['content']=={key:value[key]for key in ('text','tables','notes','continuations','visuals','reading_order')}
    assert [source['id']for source in material['sources']]==[rows[1]['id'],rows[0]['id'],rows[2]['id']]
    if case=='split_prefix':
        assert material['content']['tables'][0]['matrix'][1][0] is None
        assert material['content']['continuations'][0]['before'][0]['quote']=='Inadvertent Custody'
    if case=='reverse_notes':
        assert material['content']['tables']==[] and material['content']['notes'][0]['consumer_sources'][0]['image_position']==2
    if case=='repeated_markers':assert [note['marker']for note in material['content']['notes']]==['a','a']
    tampered=deepcopy(bundle)
    tampered['rounds'][0]['recognition']['receipt']['result']['reading_order'].reverse()
    # One-item orders need a content mutation, rather than a no-op reverse.
    if len(value['reading_order'])==1:
        tampered['rounds'][0]['recognition']['receipt']['result']['tables'][0]['matrix'][0][0]='Tampered'
    with pytest.raises(ValueError) as error:g.replay_target(rows,plan,1,tampered)
    assert not isinstance(error.value,(g.RecognitionContentError,g.ReviewRejected,g.ReviewMalformed))
