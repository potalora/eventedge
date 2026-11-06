"""Opt-in complete publication dependencies, rebuilt from retained originals.

Publication dates identify documents. They never establish availability of the
current publisher rendition. Legacy single-document graphs remain independent.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timezone, timedelta
import hashlib
import re
import time

from .regulation_evidence import (
    POLICY, MAX_ATTACHMENTS, MAX_BODY_BYTES, MAX_DETAIL_BYTES, MAX_SOURCE_BYTES,
    _ID, _json, _resource, _stamp, _text, digest, original_receipt, receipt_bytes,
)

PUBLICATION_POLICY = 'linked_publications_v1'
SOURCE_VERSION = 'regulation-publication-source-v1'
NODE_VERSION = 'regulation-publication-node-v1'
BUNDLE_VERSION = 'regulation-publication-bundle-v1'
VIEW_VERSION = 'regulation-publication-analysis-v1'
RETRIEVAL_POLICY = 'verified_government_copy_v4'
MAX_PUBLICATIONS = 8
FR_BASE = 'https://www.federalregister.gov/api/v1/documents/'
API_BASE = 'https://api.regulations.gov/v4/documents/'
FR_NUMBER = r'(?:C[1-9][0-9]{0,2}-)?20[0-9]{2}-[0-9]{5}|[0-9]{2}-[0-9]{1,6}'


def fail(reason):
    raise ValueError('invalid_regulation_publication_' + reason)


def _check(check):
    if check is not None:
        check()


def _clock(value):
    _stamp(value)
    return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc)


def _today():
    return datetime.now(timezone.utc).date().isoformat()


def _number(value):
    if type(value) is not str or not re.fullmatch(FR_NUMBER, value):
        fail('number')
    return value


def metadata_url(number):
    return FR_BASE + _number(number) + '.json'


def _linked_number(url):
    if type(url) is not str or not url.startswith(FR_BASE):
        fail('relationship_url')
    return _number(url[len(FR_BASE):].removesuffix('.json'))


def _metadata(receipt):
    raw = receipt_bytes(receipt, MAX_DETAIL_BYTES)
    value = _json(raw)
    number = _number(value.get('document_number'))
    if receipt['url'] != metadata_url(number):
        fail('metadata_url')
    try:
        publication = date.fromisoformat(value['publication_date'])
        if publication.isoformat() != value['publication_date'] or publication > _clock(receipt['observed_at']).date():
            fail('publication_date')
        if value.get('type') != 'Proposed Rule' or type(value.get('title')) is not str:
            fail('metadata_identity')
        first, last = value['start_page'], value['end_page']
        if type(first) is not int or type(last) is not int or not 0 < first <= last < first + 1000:
            fail('metadata_pages')
        parent = value['correction_of']
        parent = None if parent is None else _linked_number(parent)
        children = value['corrections']
        if type(children) is not list or len(children) >= MAX_PUBLICATIONS:
            fail('relationship_limit')
        children = [_linked_number(item) for item in children]
        if len(children) != len(set(children)) or number in children or parent == number:
            fail('relationship_cycle')
        return value, parent, children
    except (KeyError, TypeError, AttributeError):
        fail('metadata')


def _inventory(row, detail):
    """Reconcile all declared resources without treating a correction as original."""
    item = _json(receipt_bytes(detail, MAX_DETAIL_BYTES))['data']
    attrs = item['attributes']; docid = item['id']
    if item['type'] != 'documents' or type(docid) is not str or not _ID.fullmatch(docid):
        fail('catalogue')
    identity = {'document_id': docid}
    for public, api in [('title','title'),('agency_id','agencyId'),('document_type','documentType'),('posted_date','postedDate')]:
        value = attrs.get(api)
        if type(value) is not str or not value.strip() or value != row.get(public):
            fail('catalogue_identity')
        identity[public] = value
    _stamp(identity['posted_date'])
    docket = attrs.get('docketId')
    if docket is not None and (type(docket) is not str or not _ID.fullmatch(docket)):
        fail('docket')
    if row.get('document_id') != docid or row.get('docket_id') != docket:
        fail('catalogue_identity')
    if detail['url'] != API_BASE + docid + '?include=attachments':
        fail('detail_url')
    if attrs.get('withdrawn') is not False or attrs.get('restrictReason') or attrs.get('restrictReasonType'):
        fail('restricted')
    identity['docket_id'] = docket
    refs = item['relationships']['attachments']['data']; included = _json(receipt_bytes(detail, MAX_DETAIL_BYTES)).get('included', [])
    if type(refs) is not list or type(included) is not list or len(refs) > MAX_ATTACHMENTS or len(included) != len(refs):
        fail('attachments')
    ids = []
    for ref in refs:
        if (type(ref) is not dict or set(ref) != {'id','type'} or ref['type'] != 'attachments'
                or type(ref['id']) is not str or not _ID.fullmatch(ref['id']) or ref['id'] in ids or ref['id'] == docid):
            fail('attachments')
        ids.append(ref['id'])
    attachments = {}
    for item in included:
        if item.get('type') != 'attachments' or item.get('id') not in ids or item['id'] in attachments:
            fail('attachments')
        attachments[item['id']] = item['attributes']
    if set(attachments) != set(ids):
        fail('attachments')
    resources = [_resource(docid, 'primary', attrs)] + [_resource(rid,'attachment',attachments[rid]) for rid in ids]
    return identity, attrs, resources


def _printed_date(day):
    weekdays=('Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday')
    months=('January','February','March','April','May','June','July','August','September','October','November','December')
    return weekdays[day.weekday()]+', '+months[day.month-1]+' '+str(day.day)+', '+str(day.year)


def _special_copy(raw, metadata, kind, *, check=None):
    """Exact demonstrated C1 and historical part-cover forms; no browser repair."""
    from .regulation_government_copy import _WRAPPER, _pre_text, _printable
    _check(check)
    source = raw.decode('utf-8', errors='strict')
    transformations = []
    if kind == 'archived_original':
        if source.count('\x00') != 4 or '\u2400' in source:
            fail('archive_cover')
        positions = [i for i,c in enumerate(source) if c == '\x00']
        source = source.replace('\x00','\u2400')
        transformations = [{'kind':'govinfo_nul_printed_notation_v1','source_codepoint_start':i,
            'source_codepoint_end':i+1,'notation':'\u2400'} for i in positions]
    elif '\x00' in source:
        fail('correction_cover')
    wrapper = _WRAPPER.fullmatch(source)
    if wrapper is None:
        fail('framing')
    text, changes = _pre_text(wrapper['pre'],wrapper.start('pre'),check,RETRIEVAL_POLICY,sec_literals=kind=='correction')
    _printable(text)
    day = date.fromisoformat(metadata['publication_date']); display = _printed_date(day); volume = day.year-1935
    title = re.fullmatch(r'Federal Register, Volume ([0-9]+) Issue ([0-9]+) \(([^\n]+)\)',wrapper['title'])
    if title is None or int(title[1]) != volume or title[3] != display:
        fail('publication_header')
    first,last = metadata['start_page'],metadata['end_page']; pages = f'Page {first}' if first==last else f'Pages {first}-{last}'
    header = (f'\n[Federal Register Volume {volume}, Number {title[2]} ({display})]\n[Proposed Rules]\n[{pages}]\n'
        'From the Federal Register Online via the Government Publishing Office [www.gpo.gov]\n'
        f'[FR Doc No: {metadata["document_number"]}]\n')
    if not text.startswith(header) or text.count('[Federal Register Volume ') != 1:
        fail('publication_header')
    filed=day-timedelta(days=1)
    stamp=f'{filed.month}-{filed.day}-{filed.year%100:02d}'
    billing='0099-10-D' if kind=='correction' else '6560-50-P'
    footer = re.search(r'\[FR Doc\. '+re.escape(metadata['document_number'])+r' Filed '+re.escape(stamp)+
        r'; 8:45 am\]\nBILLING CODE '+billing+r'\n\s*\Z',text)
    if footer is None or text.count('[FR Doc.')!=1:
        fail('footer')
    markers=[int(x) for x in re.findall(r'\[\[Page ([0-9]+)\]\]',text)]
    if markers != ([] if kind=='correction' else list(range(first-1,last+1))):
        fail('page_continuity')
    if re.search(r'\[[^\]\n]{0,120}\bOMITTED\b[^\]\n]*\]',text,re.I):
        fail('omitted_material')
    cfr = metadata['cfr_references']
    if type(cfr) is not list or not 1 <= len(cfr) <= 16 or len({(x['title'],x['part']) for x in cfr}) != len(cfr):
        fail('authority')
    authority = 17 if kind=='correction' else 40
    if any(type(x.get('title')) is not int or x['title'] != authority or type(x.get('part')) is not str or not re.fullmatch(r'[1-9][0-9]{0,5}',x['part']) for x in cfr):
        fail('authority')
    parts = [x['part'] for x in cfr]
    printed = parts[0] if len(parts)==1 else ' and '.join(parts) if len(parts)==2 else ', '.join(parts[:-1])+', and '+parts[-1]
    citation = str(authority)+' CFR Part'+('s ' if len(parts)>1 else ' ')+printed
    if '\n'+citation+'\n' not in text:
        fail('authority')
    compact = lambda value: re.sub(r'\s+',' ',value).strip()
    rins=metadata.get('regulation_id_numbers')
    if type(rins) is not list or len(rins)!=1 or not re.fullmatch(r'[0-9]{4}-[A-Z0-9]{4}',rins[0]):
        fail('rin')
    match_title=re.search(r'\nRIN '+re.escape(rins[0])+r'\n+(?P<title>.*?)\n\n(?:Correction\n|AGENCY:)',text,re.S)
    if match_title is None or compact(match_title['title'])!=compact(metadata['title']):
        fail('title')
    if kind=='correction':
        if '\nSECURITIES AND EXCHANGE COMMISSION\n' not in text or '\nCorrection\n' not in text:
            fail('correction_kind')
        if '['+'; '.join(metadata['docket_ids'])+']' not in compact(text):
            fail('correction_dockets')
        match = re.search(r'In proposed rules document (20[0-9]{2}-[0-9]{5}) beginning on page ([0-9]+) in\s+the issue of ([A-Za-z]+, [A-Za-z]+ [0-9]{1,2}, [0-9]{4}), make the following correction:',text)
        if match is None or _linked_number(metadata['correction_of']) != match[1]:
            fail('correction_reference')
        dependency = {'fr_document_number':match[1],'first_page':int(match[2]),'printed_publication_date':match[3]}
        directive=re.search(r'\n    1\. On page ([0-9]+), in the first column, the DATES section is \n'
            r'corrected to read as follows:\n\nDATES: (?P<paragraph>[^\n]+(?:\n[^\n]+)*)\n\n(?=\[FR Doc\.)',text)
        if directive is None or int(directive[1])!=dependency['first_page']:
            fail('correction_directive')
        paragraph=compact(directive['paragraph'])
        dates=re.fullmatch(r'This release was published in the Federal Register on ([A-Za-z]+ [0-9]{1,2}, [0-9]{4})\. '
            r'Comments should be received on or before ([A-Za-z]+ [0-9]{1,2}, [0-9]{4})\.',paragraph)
        if dates is None or dates[1]!=dependency['printed_publication_date'].split(', ',1)[1]:
            fail('correction_dates')
        month_names=('January','February','March','April','May','June','July','August','September','October','November','December')
        deadline=re.fullmatch(r'([A-Za-z]+) ([0-9]{1,2}), ([0-9]{4})',dates[2])
        if deadline[1] not in month_names or date(int(deadline[3]),month_names.index(deadline[1])+1,int(deadline[2]))<day:
            fail('correction_deadline')
    else:
        if ('\nAGENCY: Environmental Protection Agency.\n' not in text
                or '\nACTION: Proposed rule.\n' not in text
                or metadata.get('action') != 'Proposed rule.'
                or not re.search(r'Part IV\n[\s\S]{0,1500}Environmental Protection Agency[\s\S]{0,1500}\u2400\u2400Federal Register / Vol\. '+str(volume),text)):
            fail('archive_cover')
        if metadata.get('docket_ids') != ['FRL-5702-1'] or '\n[FRL-5702-1]\n' not in text:
            fail('archive_authority')
        for section in ['SUMMARY','DATES','ADDRESSES','FOR FURTHER INFORMATION CONTACT','SUPPLEMENTARY INFORMATION']:
            if len(re.findall(r'(?m)^'+section+':',text))!=1:
                fail('archive_sections')
        dependency = None
    emails = any(x['kind']=='cloudflare_email_xor_v1' for x in changes)
    if bool(wrapper['script']) != emails:
        fail('email_script')
    if wrapper['script']:
        changes.append({'kind':'cloudflare_email_script_accounted_v1','source_codepoint_start':wrapper.start('script'),
            'source_codepoint_end':wrapper.end('script')})
    for item, position in zip(transformations,(i for i,c in enumerate(text) if c=='\u2400')):
        item.update(text_codepoint_start=position,text_codepoint_end=position+1)
    if '[GRAPHIC]' in text or metadata.get('images'):
        fail('unsupported_special_graphics')
    return {'text':text,'transformations':transformations+changes,'dependency':dependency,
            'representation':'preserves_preformatted_layout','extraction_version':'linked-publication-pre-v1'}


def build_publication_node(capture, *, check=None):
    """Reextract complete resources. A previous node status confers no authority."""
    try:
        _check(check)
        if type(capture) is not dict or set(capture) != {'row','detail','metadata','bodies','primary_attempt','graphics'}:
            fail('capture')
        catalogue, attrs, resources = _inventory(capture['row'],capture['detail'])
        metadata,parent,children = _metadata(capture['metadata'])
        number = attrs.get('frDocNum'); normalized = number.replace('\u2013','-') if type(number) is str else None
        if normalized != metadata['document_number']:
            fail('crosswalk_number')
        historical = date.fromisoformat(metadata['publication_date']).year < 2000
        kind = 'correction' if parent is not None else 'archived_original' if historical else 'original'
        agency = catalogue['agency_id']
        agencies = metadata['agencies']
        # This exact legacy record includes an unclassified cover token "Rule".
        # Retain it in the raw metadata; it is never an additional agency.
        if kind == 'archived_original' and agencies[:1] == [{'raw_name':'Rule'}]:
            agencies = agencies[1:]
        agency_names = [a['name'] for a in agencies]
        expected_names = {'SEC':[['Securities and Exchange Commission']], 'EPA':[['Environmental Protection Agency']],
            'CFPB':[['Consumer Financial Protection Bureau']], 'FTC':[['Federal Trade Commission']],
            'FDA':[['Food and Drug Administration'],['Health and Human Services Department','Food and Drug Administration']]}
        if agency not in expected_names or agency_names not in expected_names[agency]:
            fail('crosswalk_agency')
        catalogue_title=catalogue['title'].replace(':',';') if agency=='SEC' else catalogue['title']
        if agency=='CFPB' and catalogue_title=='Request for Information: Promoting Access to Mortgage Credit':
            catalogue_title='Request for Information Regarding Promoting Access to Mortgage Credit'
        if metadata['title'] != catalogue_title or catalogue['document_type'] != metadata['type']:
            fail('crosswalk_title')
        mapping = metadata.get('regulations_dot_gov_info',{})
        if kind != 'archived_original' and (mapping.get('document_id') != catalogue['document_id'] or mapping.get('agency_id') != agency):
            fail('crosswalk_document')
        if kind=='archived_original':
            if (agency!='EPA' or attrs.get('startEndPage') != f'{metadata["start_page"]} - {metadata["end_page"]}'
                    or attrs.get('pageCount') != metadata['end_page']-metadata['start_page']+1
                    or metadata['publication_date'] >= catalogue['posted_date'][:10]):
                fail('archive_identity')
        elif metadata['publication_date'] != catalogue['posted_date'][:10]:
            fail('publication_date')
        bodies = capture['bodies']; docid = catalogue['document_id']
        if type(bodies) is not dict or set(bodies) != {x['resource_id'] for x in resources}:
            fail('missing_body')
        primary = resources[0]; attempt = capture['primary_attempt']
        if (type(attempt) is not dict or set(attempt) != {'resource_id','http_status','complete','receipt'}
                or attempt['resource_id'] != docid or attempt['complete'] is not True
                or attempt['http_status'] not in {200,403,404,410}
                or attempt['receipt']['url'] != primary['selected']['fileUrl']):
            fail('primary_attempt')
        original = receipt_bytes(attempt['receipt'],MAX_BODY_BYTES,allow_empty=True)
        if attempt['http_status']==200 and len(original)!=primary['selected']['size']:
            fail('primary_size')
        identity = {'fr_document_number':metadata['document_number'],'publication_date':metadata['publication_date'],
            'agency_id':agency,'title':metadata['title'],'first_page':metadata['start_page'],'last_page':metadata['end_page'],
            'notice_action':'Correction' if kind=='correction' else metadata.get('action')}
        units = []
        for resource in resources:
            _check(check)
            rid = resource['resource_id']; receipt = bodies[rid]; raw = receipt_bytes(receipt,MAX_BODY_BYTES)
            if resource['role']=='primary':
                expected = 'https://www.govinfo.gov/content/pkg/FR-'+identity['publication_date']+'/html/'+identity['fr_document_number']+'.htm'
                if receipt['url'] != expected:
                    fail('body_url')
                if kind=='original':
                    from .regulation_government_copy import extract_government_copy
                    copy_crosswalk=capture['metadata'] if agency in {'SEC','FTC'} else None
                    proof = extract_government_copy(receipt_bytes(capture['detail'],MAX_DETAIL_BYTES),raw,
                        retrieval_policy=RETRIEVAL_POLICY,crosswalk_receipt=copy_crosswalk,
                        graphics_receipts=capture['graphics'],check=check)
                    # A relationship receipt may carry optional authority
                    # descriptors. Their presence cannot contradict the body
                    # that the original-copy parser has independently bound.
                    if metadata.get('action') is not None and metadata['action']!=proof['notice_metadata']['action']:
                        fail('metadata_action')
                    cfr=metadata.get('cfr_references')
                    if cfr:
                        title,printed=re.fullmatch(r'([0-9]+) CFR Parts? (.+)',proof['identity']['cfr_part']).groups()
                        expected=[(int(title),p) for p in re.findall(r'[0-9]+',printed)]
                        actual=[(r.get('title'),r.get('part')) for r in cfr]
                        if actual!=expected:fail('metadata_cfr')
                    dockets=metadata.get('docket_ids')
                    if dockets and agency!='SEC':
                        if (type(dockets) is not list or any(type(d) is not str or not d or len(d)>256 for d in dockets)
                                or len(set(dockets))!=len(dockets)
                                or proof['identity']['docket_id'] not in [d.removeprefix('Docket No. ') for d in dockets]
                                or any(not re.search(r'(?<![A-Za-z0-9-])'+re.escape(d)+r'(?![A-Za-z0-9-])',
                                    re.sub(r'\s+',' ',proof['text'])) for d in dockets)):
                            fail('metadata_docket')
                else:
                    if capture['graphics']:
                        fail('unexpected_graphics')
                    proof = _special_copy(raw,metadata,kind,check=check)
                text = proof['text']; representation = 'verified_government_copy_with_graphics' if proof.get('graphics') else 'verified_government_copy_text'
            else:
                if receipt['url'] != resource['selected']['fileUrl'] or len(raw) != resource['selected']['size']:
                    fail('attachment_body')
                if resource['selected']['format']=='pdf':
                    fail('unsupported_attachment_pdf')
                text = _text(raw,resource['selected']['format'],check); proof = None; representation = 'full_attachment_text'
            unit = {'resource_id':rid,'role':resource['role'],'receipt':receipt,'text':text,
                'text_sha256':hashlib.sha256(text.encode()).hexdigest(),'representation':representation}
            unit['unit_id'] = 'publication:'+identity['fr_document_number']+':'+rid+':'+digest(unit)
            if proof is not None:
                unit['government_copy'] = proof
            units.append(unit)
        if sum(len(u['text'].encode()) for u in units)>8*1024**2:
            fail('text_limit')
        value = {'version':NODE_VERSION,'kind':kind,'identity':identity,'catalogue_identity':catalogue,
            'capture':deepcopy(capture),'resources':resources,'units':units,'parent':parent,'corrections':children,
            'rendition_basis':'publisher_rendition_at_acquisition_not_historical_as_published'}
        _check(check)
        return {**value,'manifest_sha256':digest(value)}
    except (KeyError,TypeError,AttributeError,UnicodeError,OverflowError) as error:
        fail('node')


def _all_receipts(value):
    if type(value) is dict:
        if set(value)=={'url','observed_at','sha256','bytes','raw_base64'}:
            yield value
        else:
            for child in value.values():
                yield from _all_receipts(child)
    elif type(value) is list:
        for child in value:
            yield from _all_receipts(child)


def build_publication_bundle(row,nodes,closing_receipt,*,sealed_at,check=None):
    _check(check)
    if type(nodes) is not dict or not 1 <= len(nodes) <= MAX_PUBLICATIONS:
        fail('node_limit')
    by_number = {}
    for ref,node in nodes.items():
        expected = build_publication_node(node['capture'],check=check)
        if expected != node or ref != node['manifest_sha256']:
            fail('node_changed')
        number = node['identity']['fr_document_number']
        if number in by_number:
            fail('duplicate_node')
        by_number[number] = ref
    originals = [n for n in nodes.values() if n['parent'] is None]
    roots = [n for n in nodes.values() if n['catalogue_identity']['document_id']==row.get('document_id')]
    if len(originals)!=1 or len(roots)!=1 or roots[0]['catalogue_identity'] != {k:row.get(k) for k in roots[0]['catalogue_identity']}:
        fail('root')
    original = originals[0]; closing,parent,children = _metadata(closing_receipt)
    if (closing['document_number'] != original['identity']['fr_document_number'] or parent is not None
            or children != original['corrections']):
        fail('relationship_changed')
    initial,_,_=_metadata(original['capture']['metadata'])
    if any(closing.get(field)!=initial.get(field) for field in ['publication_date','title','type','action',
            'start_page','end_page','agencies','cfr_references','docket_ids','regulation_id_numbers','regulations_dot_gov_info']):
        fail('closing_identity_changed')
    edges = []
    for node in nodes.values():
        if node['parent'] is not None:
            if node['parent'] not in by_number:
                fail('missing_original')
            target = nodes[by_number[node['parent']]]
            if (node['identity']['fr_document_number'] not in target['corrections']
                    or target['identity']['agency_id'] != node['identity']['agency_id']
                    or target['kind']!='original'
                    or target['identity']['publication_date'] > node['identity']['publication_date']):
                fail('relationship_mismatch')
            dependency = node['units'][0]['government_copy']['dependency']
            parent_metadata,_,_=_metadata(target['capture']['metadata'])
            child_metadata,_,_=_metadata(node['capture']['metadata'])
            if any(parent_metadata.get(field)!=child_metadata.get(field) for field in
                    ['cfr_references','docket_ids','regulation_id_numbers']):
                fail('correction_authority')
            if (dependency['first_page']!=target['identity']['first_page']
                    or dependency['printed_publication_date']!=_printed_date(date.fromisoformat(target['identity']['publication_date']))):
                fail('correction_reference')
            edges.append({'kind':'corrects','from':node['manifest_sha256'],'to':target['manifest_sha256']})
        if set(node['corrections']) != {n['identity']['fr_document_number'] for n in nodes.values() if n['parent']==node['identity']['fr_document_number']}:
            fail('missing_correction')
    dependency_clocks=[_clock(r['observed_at']) for r in _all_receipts(list(nodes.values()))]
    if not dependency_clocks or _clock(closing_receipt['observed_at'])<max(dependency_clocks):
        fail('closing_clock')
    clocks=dependency_clocks+[_clock(closing_receipt['observed_at'])]
    if _clock(sealed_at) < max(clocks):
        fail('availability')
    kind = 'historical_repost' if roots[0]['kind']=='archived_original' else roots[0]['kind']
    value = {'version':BUNDLE_VERSION,'policy':POLICY,'publication_policy':PUBLICATION_POLICY,
        'identity':deepcopy(roots[0]['catalogue_identity']),'node_refs':sorted(nodes),'edges':sorted(edges,key=lambda e:e['from']),
        'closing_relationship_receipt':closing_receipt,'available_at':sealed_at,'publication_kind':kind,
        'execution_authority':'none' if kind=='historical_repost' else 'requires_separate_assessment_and_verified_target',
        'content_status':'complete','unresolved_material_dependencies':[]}
    _check(check)
    return {**value,'manifest_sha256':digest(value)}


def validate_bundle(bundle,nodes):
    try:
        selected = {ref:nodes[ref] for ref in bundle['node_refs']}
        expected = build_publication_bundle(bundle['identity'],selected,bundle['closing_relationship_receipt'],sealed_at=bundle['available_at'])
        if expected != bundle:
            fail('bundle_changed')
        return bundle
    except (KeyError,TypeError,AttributeError):
        fail('bundle')


def _discovery_rows(item):
    attrs=item['attributes']
    return {'document_id':item['id'],'title':attrs['title'],'agency_id':attrs['agencyId'],
        'document_type':attrs['documentType'],'posted_date':attrs['postedDate'],
        'comment_end_date':attrs.get('commentEndDate',''),'summary':(attrs.get('summary') or '')[:500],
        'docket_id':attrs.get('docketId','')}


def _replay_discovery(payload,graph):
    """Reconcile the complete selected population against raw publisher pages."""
    coverage=payload['coverage'];traces=graph['discovery_receipts'];actual=payload['proposed_rules']
    if type(traces) is not list or len(traces)>3200:
        fail('discovery_trace')
    for trace in traces:
        receipt_bytes(trace['receipt'],MAX_SOURCE_BYTES)
        if _clock(trace['receipt']['observed_at'])>_clock(graph['discovery_acquired_at']):
            fail('discovery_clock')
    if coverage.get('complete') is not True:
        # Incomplete discovery remains an unavailable source. Raw failed pages
        # are retained without pretending their invalid JSON proves a population.
        return
    expected=[]
    if coverage.get('mode')=='explicit_document_ids':
        ids=coverage.get('document_ids')
        if (type(ids) is not list or not 1<=len(ids)<=3 or len(set(ids))!=len(ids)
                or coverage.get('exhaustive_recent_discovery') is not False
                or len(traces)!=len(ids) or coverage.get('as_of')!=graph['as_of']):
            fail('named_discovery')
        for docid,trace in zip(ids,traces):
            if set(trace)!={'document_id','receipt'} or trace['document_id']!=docid:
                fail('named_discovery')
            receipt=trace['receipt']
            if receipt['url']!=API_BASE+docid+'?include=attachments':
                fail('named_discovery_url')
            item=_json(receipt_bytes(receipt,MAX_DETAIL_BYTES))['data']
            if item['id']!=docid or item['type']!='documents':
                fail('named_discovery_identity')
            row=_discovery_rows(item)
            expected.append({k:row[k] for k in ['document_id','title','agency_id','document_type','posted_date','docket_id']})
    else:
        if coverage.get('mode')!='exhaustive_window' or coverage.get('as_of')!=graph['as_of']:
            fail('discovery_mode')
        samples=coverage.get('samples')
        if type(samples) is not dict or not samples or len(samples)>16:
            fail('discovery_samples')
        groups={}
        for trace in traces:
            if set(trace)!={'agency_id','page','page_size','receipt'} or trace['receipt']['url']!=API_BASE.removesuffix('/'):
                fail('discovery_page')
            key=trace['agency_id'] or 'all_agencies';groups.setdefault(key,[]).append(trace)
        if set(groups)!=set(samples):
            fail('discovery_samples')
        for key,pages in groups.items():
            sample=samples[key];agency=pages[0]['agency_id'];start=sample.get('posted_date_from')
            if (sample.get('complete') is not True or sample.get('mode')!='exhaustive_window'
                    or sample.get('agency')!=agency or sample.get('posted_date_to')!=graph['as_of']
                    or type(start) is not str or date.fromisoformat(start).isoformat()!=start
                    or start>graph['as_of'] or sample.get('pages')!=len(pages)):
                fail('discovery_window')
            seen=set();previous=None;pagination=None;terminated=False
            for index,trace in enumerate(pages,1):
                if terminated or trace['page']!=index or type(trace['page_size']) is not int or not 1<=trace['page_size']<=250:
                    fail('discovery_page')
                raw=_json(receipt_bytes(trace['receipt'],MAX_SOURCE_BYTES));data=raw['data'];meta=raw['meta']
                totals=(meta['totalPages'],meta.get('totalElements'))
                if (type(data) is not list or type(totals[0]) is not int or totals[0]<0
                        or totals[0] and index>totals[0]
                        or totals[1] is not None and (type(totals[1]) is not int or totals[1]<0)
                        or pagination is not None and pagination!=totals
                        or totals[0]==0 and (data or totals[1] not in (None,0))):
                    fail('discovery_pagination')
                pagination=totals;older=False
                for item in data:
                    row=_discovery_rows(item);day=row['posted_date'][:10]
                    _stamp(row['posted_date'])
                    if (not _ID.fullmatch(row['document_id']) or row['document_id'] in seen
                            or not all(type(row[f]) is str and row[f].strip() for f in ['title','agency_id','document_type'])
                            or row['document_type']!='Proposed Rule' or agency and row['agency_id']!=agency
                            or previous is not None and day>previous):
                        fail('discovery_identity')
                    seen.add(row['document_id']);previous=day;older|=day<start
                    if start<=day<=graph['as_of']:expected.append(row)
                terminal=index>=totals[0]
                if totals[1] is not None and (len(seen)>totals[1] or terminal and len(seen)!=totals[1]):
                    fail('discovery_totals')
                terminated=older or terminal
                if index==len(pages):
                    proof={'mode':'exhaustive_window','complete':True,'agency':agency,
                        'posted_date_from':start,'posted_date_to':graph['as_of'],'pages':index,
                        'total':totals[1],'has_next':not terminal,'termination':'older_than_window' if older else 'terminal_page'}
                    if not terminated or proof!=sample:fail('discovery_boundary')
    if len(expected)!=len(actual) or any(any(row.get(k)!=v for k,v in raw.items()) for raw,row in zip(expected,actual)):
        fail('discovery_population')


def _summary(payload, *, check=None):
    graph = payload['regulation_evidence']; rows = payload['proposed_rules']
    if (type(graph) is not dict or set(graph) != {'version','policy','publication_policy','retrieval_policy',
            'publication_nodes','corpus','acquisition_failures','rows','coverage','as_of','discovery_acquired_at',
            'discovery_receipts','manifest_sha256'}
            or graph['version'] != SOURCE_VERSION or graph['policy'] != POLICY
            or graph['publication_policy'] != PUBLICATION_POLICY or graph['retrieval_policy'] != RETRIEVAL_POLICY
            or graph['manifest_sha256'] != digest({k:v for k,v in graph.items() if k!='manifest_sha256'})
            or type(rows) is not list or len(rows)>50000
            or graph['rows'] != [{'index':i,'document_id':r['document_id'],'row_sha256':digest(r)} for i,r in enumerate(rows)]):
        fail('source')
    date.fromisoformat(graph['as_of'])
    if graph['as_of']>_clock(graph['discovery_acquired_at']).date().isoformat():
        fail('future_selection')
    _replay_discovery(payload,graph)
    nodes,corpus,failures,coverage = (graph[k] for k in ['publication_nodes','corpus','acquisition_failures','coverage'])
    if any(type(v) is not dict for v in [nodes,corpus,failures,coverage]) or len(nodes)>256:
        fail('source_nodes')
    if len({node['identity']['fr_document_number'] for node in nodes.values()})!=len(nodes):
        fail('conflicting_source_renditions')
    used = set(); identifiers = {r['document_id'] for r in rows}
    if set(corpus) & set(failures) or set(corpus)|set(failures) != identifiers:
        fail('source_partition')
    for docid,bundle in corpus.items():
        _check(check)
        validate_bundle(bundle,nodes); used.update(bundle['node_refs'])
        if bundle['identity']['document_id']!=docid:
            fail('source_root')
    if set(nodes)!=used:
        fail('orphan_node')
    for node in nodes.values():
        if node['identity']['publication_date']>graph['as_of']:
            fail('future_publication')
    for docid,failed in failures.items():
        if (type(failed) is not dict or set(failed)!={'identity','failure','receipts'}
                or failed['identity']['document_id']!=docid or type(failed['receipts']) is not list
                or type(failed['failure']) is not dict or set(failed['failure'])!={'code','reason_code','http_status'}
                or type(failed['failure']['code']) is not str or not failed['failure']['code']
                or len(failed['failure']['code'])>160
                or failed['failure']['reason_code'] not in {'http_error','timeout','transport_error','invalid_response','provider_error','provider_maintenance','batch_failure'}):
            fail('source_failure')
        for receipt in failed['receipts']:
            receipt_bytes(receipt,MAX_BODY_BYTES,allow_empty=True)
    complete = 0
    for row in rows:
        docid = row['document_id']
        if row.get('regulation_evidence_ref')!=docid:
            fail('source_row')
        if docid in corpus:
            b = corpus[docid]
            if (row.get('regulation_evidence_status')!='complete' or row.get('regulation_failure') is not None
                    or b['identity']!={k:row.get(k) for k in b['identity']}
                    or row.get('available_at')!=b['available_at'] or row.get('publication_kind')!=b['publication_kind']
                    or row.get('regulation_publication_policy')!=PUBLICATION_POLICY):
                fail('source_row')
            complete+=1
        elif (row.get('regulation_evidence_status')!='failed' or row.get('regulation_failure')!=failures[docid]['failure']):
            fail('source_failure_row')
    keys = {'discovery_complete','content_complete','required_rows','complete_rows','failed_rows',
            'consumed_bytes','reserved_bytes','byte_limit','byte_budget_exhausted'}
    if (set(coverage)!=keys or type(payload.get('coverage')) is not dict
            or type(payload['coverage'].get('complete')) is not bool
            or any(type(coverage[k]) is not bool for k in ['discovery_complete','content_complete','byte_budget_exhausted'])
            or any(type(coverage[k]) is not int or coverage[k]<0 for k in keys-{'discovery_complete','content_complete','byte_budget_exhausted'})
            or coverage['discovery_complete']!=payload['coverage']['complete']
            or coverage['required_rows']!=len(rows) or coverage['complete_rows']!=complete
            or coverage['failed_rows']!=len(rows)-complete
            or not 0<coverage['byte_limit']<=MAX_SOURCE_BYTES
            or coverage['consumed_bytes']+coverage['reserved_bytes']>coverage['byte_limit']
            or coverage['content_complete'] is not (coverage['discovery_complete'] and not failures and not coverage['byte_budget_exhausted'])):
        fail('source_coverage')
    # Receipts can be referenced in several nodes/proofs. Account each captured
    # URL/time/body only once, while acquisition charges every physical request.
    receipts = {(r['url'],r['observed_at'],r['sha256']):r for r in _all_receipts(graph)}
    if sum(r['bytes'] for r in receipts.values())>coverage['consumed_bytes']:
        fail('source_bytes')
    result={**coverage,'policy':POLICY,'retrieval_policy':RETRIEVAL_POLICY,'publication_policy':PUBLICATION_POLICY,
        'manifest_sha256':graph['manifest_sha256'],'evidence_sha256':{k:v['manifest_sha256'] for k,v in corpus.items()},
        'failed_document_ids':sorted(failures)}
    if 'regulation_acquisition_failure' in payload:
        marker=payload['regulation_acquisition_failure']
        if marker!={'version':'regulation-publication-acquisition-failure-v1','reason_code':'timeout',
                    'boundary':'source_publication','graph_manifest_sha256':graph['manifest_sha256']}:
            fail('acquisition_marker')
        result.update(raw_content_complete=coverage['content_complete'],content_complete=False,acquisition_complete=False,
            acquisition_failure=deepcopy(marker))
    return result


def validate_publication_source(payload):
    try:
        return _summary(payload)
    except (KeyError,TypeError,AttributeError,UnicodeError,OverflowError):
        fail('source')


def _view(bundle,nodes):
    validate_bundle(bundle,nodes)
    selected = {ref:nodes[ref] for ref in bundle['node_refs']}
    units,resources = [],[]
    for ref,node in sorted(selected.items(),key=lambda item:(item[1]['identity']['publication_date'],item[1]['identity']['fr_document_number'])):
        prefix = node['identity']['fr_document_number']+':'
        for unit in node['units']:
            item=deepcopy(unit);item['resource_id']=prefix+item['resource_id']
            item['publication_ref']=ref;item['publication_kind']=node['kind'];units.append(item)
        for resource in node['resources']:
            item=deepcopy(resource);item['resource_id']=prefix+item['resource_id'];resources.append(item)
    value = {'version':VIEW_VERSION,'policy':POLICY,'publication_policy':PUBLICATION_POLICY,
        'identity':deepcopy(bundle['identity']),'bundle':deepcopy(bundle),'publication_nodes':deepcopy(selected),
        'detail_receipt':next(n['capture']['detail'] for n in selected.values()
            if n['catalogue_identity']['document_id']==bundle['identity']['document_id']),
        'units':units,'resources':resources,'content_status':'complete','unresolved_material_dependencies':[]}
    return {**value,'manifest_sha256':digest(value)}


def analysis_view(payload,discovery_id):
    if validate_publication_source(payload)['content_complete'] is not True:
        fail('incomplete_source')
    graph=payload['regulation_evidence']
    return _view(graph['corpus'][discovery_id],graph['publication_nodes'])


def validate_analysis_view(value):
    try:
        expected=_view(value['bundle'],value['publication_nodes'])
        if value!=expected:
            fail('analysis_view')
        return value
    except (KeyError,TypeError,AttributeError):
        fail('analysis_view')


def validate_material(value):
    if type(value) is dict and value.get('version')==VIEW_VERSION:
        return validate_analysis_view(value)
    from .regulation_evidence import validate_regulation_evidence
    return validate_regulation_evidence(value)


def acquire_publication_rules(source,agencies=None,days_back=30,*,as_of=None,retrieval_policy=None):
    """One bounded provider/byte owner for discovery and all publication edges."""
    from . import regulations_source as transport
    from .evidence import CoverageRecords,current_session_date
    from .request_policy import current_provider_deadline,provider_budget,provider_timeout
    from .fetch_errors import SourceFetchError,source_fetch_error
    if retrieval_policy!=RETRIEVAL_POLICY:
        fail('retrieval_policy')
    if current_provider_deadline('regulations') is None:
        with provider_budget('regulations',time.monotonic()+180):
            return acquire_publication_rules(source,agencies,days_back,as_of=as_of,retrieval_policy=retrieval_policy)
    if transport._CONTENT_BUDGET.get() is not None:
        fail('nested_acquisition')
    as_of=as_of or current_session_date();date.fromisoformat(as_of)
    if as_of>_today():
        fail('future_selection')
    if agencies is not None and (type(agencies) is not list or not 1<=len(agencies)<=16
            or any(type(a) is not str or not _ID.fullmatch(a) for a in agencies) or len(set(agencies))!=len(agencies)):
        fail('agency_scope')
    owner={'limit':transport.MAX_REGULATION_SOURCE_BYTES,'used':0,'reserved':0,'exhausted':False}
    token=transport._CONTENT_BUDGET.set(owner)
    nodes,corpus,failures,node_cache={},{},{},{}
    check=lambda:provider_timeout('regulations')
    def now():return datetime.now(timezone.utc).isoformat()
    def read(url,*,api=False,maximum=MAX_BODY_BYTES,retained=None):
        check();raw=source._content_request(url,maximum=maximum,api=api)
        receipt=original_receipt(raw,observed_at=now(),url=url+'?include=attachments' if api else url)
        if retained is not None:retained.append(receipt)
        return receipt
    try:
        traces=[];trace_token=transport._PUBLICATION_DISCOVERY_TRACE.set(traces)
        try:
            try:
                discovered=source.get_recent_proposed_rules(agencies,days_back,as_of=as_of)
                rows=deepcopy(list(discovered));discovery=deepcopy(discovered.coverage)
            except SourceFetchError as error:
                rows=deepcopy(error.partial_data.get('proposed_rules',[]));discovery=deepcopy(error.partial_data.get('coverage',{'complete':False}))
        finally:
            transport._PUBLICATION_DISCOVERY_TRACE.reset(trace_token)
        if discovery.get('mode')=='explicit_document_ids':
            traces=deepcopy(getattr(source,'publication_discovery_receipts',[]))
        discovery_acquired_at=now()
        seen={}
        for row in rows:
            docid=row['document_id'];retained=[]
            try:
                check()
                if docid in seen:
                    if seen[docid]!={k:v for k,v in row.items() if not k.startswith('regulation_')}:
                        fail('duplicate_discovery')
                    continue
                seen[docid]=deepcopy(row)
                # One captured rendition per FR identity per acquisition. A
                # discovered original and its correction retain distinct root
                # bundles, sharing the same complete material and graphics.
                cached=[n for n in node_cache.values() if n['catalogue_identity']['document_id']==docid]
                if cached:
                    if len(cached)!=1 or cached[0]['catalogue_identity']!={k:row.get(k) for k in cached[0]['catalogue_identity']}:
                        fail('cached_identity_conflict')
                    selected={};pending=[cached[0]['identity']['fr_document_number']]
                    while pending:
                        current=pending.pop(0)
                        if current not in node_cache:fail('cached_closure_incomplete')
                        node=node_cache[current]
                        if node['manifest_sha256'] in selected:continue
                        selected[node['manifest_sha256']]=node
                        pending.extend(([node['parent']] if node['parent'] else [])+node['corrections'])
                    originals=[n for n in selected.values() if n['parent'] is None]
                    if len(originals)!=1:fail('cached_root')
                    closing=read(metadata_url(originals[0]['identity']['fr_document_number']),maximum=MAX_DETAIL_BYTES,retained=retained)
                    bundle=build_publication_bundle(row,selected,closing,sealed_at=now(),check=check)
                    corpus[docid]=bundle
                    continue
                detail=read(API_BASE+docid,api=True,maximum=MAX_DETAIL_BYTES,retained=retained)
                attrs=_json(receipt_bytes(detail,MAX_DETAIL_BYTES))['data']['attributes']
                number=_number(attrs['frDocNum'].replace('\u2013','-'))
                pending=[number];metadata={};details={number:(row,detail)}
                while pending:
                    check();current=pending.pop(0)
                    if current in metadata:continue
                    if len(metadata)>=MAX_PUBLICATIONS:fail('node_limit')
                    receipt=read(metadata_url(current),maximum=MAX_DETAIL_BYTES,retained=retained)
                    value,parent,children=_metadata(receipt)
                    if value['publication_date']>as_of:fail('future_publication')
                    metadata[current]=receipt
                    pending.extend(x for x in ([parent] if parent else [])+children if x not in metadata)
                selected={}
                for current,receipt in metadata.items():
                    value,_,_=_metadata(receipt)
                    if current not in details:
                        mapping=value.get('regulations_dot_gov_info',{});regid=mapping.get('document_id')
                        if type(regid) is not str or not _ID.fullmatch(regid):fail('missing_catalogue_mapping')
                        dependency=read(API_BASE+regid,api=True,maximum=MAX_DETAIL_BYTES,retained=retained)
                        a=_json(receipt_bytes(dependency,MAX_DETAIL_BYTES))['data']['attributes']
                        r={k:a.get(v) for k,v in [('title','title'),('agency_id','agencyId'),('document_type','documentType'),('posted_date','postedDate'),('docket_id','docketId')]};r['document_id']=regid
                        details[current]=(r,dependency)
                    r,d=details[current];_,_,inventory=_inventory(r,d)
                    primary=inventory[0];attempt={}
                    try:
                        source._content_request(primary['selected']['fileUrl'],maximum=MAX_BODY_BYTES,capture=attempt)
                    except SourceFetchError:
                        if not attempt or attempt.get('complete') is not True or attempt.get('http_status') not in {403,404,410}:raise
                    finally:
                        if attempt:retained.append(attempt['receipt'])
                    body_url='https://www.govinfo.gov/content/pkg/FR-'+value['publication_date']+'/html/'+current+'.htm'
                    bodies={r['document_id']:read(body_url,retained=retained)}
                    for resource in inventory[1:]:
                        bodies[resource['resource_id']]=read(resource['selected']['fileUrl'],retained=retained)
                    graphics={}
                    if value.get('correction_of') is None and date.fromisoformat(value['publication_date']).year>=2000:
                        from .regulation_government_copy import government_graphics_plan,MAX_GRAPHIC_BYTES
                        plan=government_graphics_plan(receipt_bytes(d,MAX_DETAIL_BYTES),receipt_bytes(bodies[r['document_id']],MAX_BODY_BYTES),
                            retrieval_policy=RETRIEVAL_POLICY,crosswalk_receipt=receipt if r['agency_id'] in {'SEC','FTC'} else None,check=check)
                        for graphic in plan:
                            graphics[graphic['graphic_id']]=read(graphic['url'],maximum=MAX_GRAPHIC_BYTES,retained=retained)
                    node=build_publication_node({'row':r,'detail':d,'metadata':receipt,'bodies':bodies,
                        'primary_attempt':{'resource_id':r['document_id'],**attempt},'graphics':graphics},check=check)
                    selected[node['manifest_sha256']]=node
                originals=[n for n in selected.values() if n['parent'] is None]
                if len(originals)!=1:fail('root')
                closing=read(metadata_url(originals[0]['identity']['fr_document_number']),maximum=MAX_DETAIL_BYTES,retained=retained)
                bundle=build_publication_bundle(row,selected,closing,sealed_at=now(),check=check)
                if len(set(nodes)|set(selected))>256:fail('source_node_limit')
                for node in selected.values():
                    number=node['identity']['fr_document_number']
                    if number in node_cache and node_cache[number]!=node:
                        fail('conflicting_acquired_renditions')
                    node_cache[number]=node
                nodes.update(selected);corpus[docid]=bundle
            except Exception as error:
                failure=source_fetch_error('Regulatory publication dependency unavailable',error)
                code=str(error) if isinstance(error,ValueError) and str(error).startswith(('invalid_regulation_','unsupported_regulation_','missing_regulation_')) else failure.reason_code
                corpus.pop(docid,None)
                failures[docid]={'identity':deepcopy(row),'receipts':retained,
                    'failure':{'code':code[:160],'reason_code':failure.reason_code,'http_status':failure.http_status}}
        # Only successful bundles retain node authority. Partial request bytes
        # remain in acquisition_failures and never become independent discoveries.
        used={ref for b in corpus.values() for ref in b['node_refs']};nodes={k:v for k,v in nodes.items() if k in used}
        for row in rows:
            docid=row['document_id'];row['regulation_evidence_ref']=docid
            if docid in corpus:
                b=corpus[docid];row.update(regulation_evidence_status='complete',regulation_publication_policy=PUBLICATION_POLICY,
                    available_at=b['available_at'],publication_kind=b['publication_kind'])
            else:row.update(regulation_evidence_status='failed',regulation_failure=failures[docid]['failure'])
        coverage={'discovery_complete':discovery.get('complete') is True,
            'content_complete':discovery.get('complete') is True and not failures and not owner['exhausted'],
            'required_rows':len(rows),'complete_rows':sum(r['document_id'] in corpus for r in rows),
            'failed_rows':sum(r['document_id'] not in corpus for r in rows),'consumed_bytes':owner['used'],
            'reserved_bytes':owner['reserved'],'byte_limit':owner['limit'],'byte_budget_exhausted':owner['exhausted']}
        graph={'version':SOURCE_VERSION,'policy':POLICY,'publication_policy':PUBLICATION_POLICY,'retrieval_policy':RETRIEVAL_POLICY,
            'publication_nodes':nodes,'corpus':corpus,'acquisition_failures':failures,'as_of':as_of,
            'discovery_acquired_at':discovery_acquired_at,
            'discovery_receipts':traces,
            'rows':[{'index':i,'document_id':r['document_id'],'row_sha256':digest(r)} for i,r in enumerate(rows)],'coverage':coverage}
        graph['manifest_sha256']=digest(graph);payload={'proposed_rules':rows,'coverage':discovery,'regulation_evidence':graph}
        try:check()
        except SourceFetchError as error:
            # Never publish a completed graph after the inherited deadline.
            marker={'version':'regulation-publication-acquisition-failure-v1','reason_code':'timeout',
                'boundary':'source_publication','graph_manifest_sha256':graph['manifest_sha256']}
            raise SourceFetchError('Regulatory publication deadline exhausted',reason_code='timeout',
                partial_data={**payload,'regulation_acquisition_failure':marker}) from error
        if not coverage['content_complete']:
            raise SourceFetchError('Regulatory publication dependencies incomplete',reason_code='batch_failure',partial_data=payload)
        result=CoverageRecords(rows,coverage=discovery);result.regulation_evidence=graph
        return result
    finally:
        transport._CONTENT_BUDGET.reset(token)
