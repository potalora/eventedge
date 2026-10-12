"""Bounded, source-bound ownership lineage; independent of annual comparators.

No acquisition occurs here. Facts must originate from ``ownership_facts`` at the
raw parser boundary. Durable acceptance uses ``validate_ownership_lineage`` with
retained raw primary bodies, rather than trusting a self-hashed facts receipt.
A schema previousAccessionNumber is a candidate locator, never an assertion of
base, immediate predecessor, or complete amendment coverage.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import re
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET


POLICY = 'complete_ownership_lineage_v1'
POLICY_V2 = 'complete_ownership_lineage_v2'
UNIT_REFERENCE = 'complete_selected_unit_v1'
MAX_INLINE_REFERENCE_BYTES = 65536
_ACCESSION = re.compile(r'[0-9]{10}-[0-9]{2}-[0-9]{6}')
_FAMILIES = {'SCHEDULE 13D', 'SCHEDULE 13G'}


class OwnershipLineageError(ValueError):
    """Fixed diagnostic code, without publisher text or credentials."""


def normalize_filing_form(value):
    return re.sub(r'^SC (13[DG](?:/A)?)$', r'SCHEDULE \1', ' '.join(value.upper().split()))


def filing_form_family(value):
    return normalize_filing_form(value).removesuffix('/A')


def _cik(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{1,10}', value) or int(value) == 0:
        raise OwnershipLineageError('invalid_ownership_cik')
    return value.zfill(10)


def ownership_evidence_digest(evidence):
    """Immutable source identity, unaffected by optional evidence receipts."""
    keys = ('version', 'format', 'accession', 'form', 'filing_date', 'accepted_at',
            'submission_sha256', 'header_sha256', 'roles', 'issuer_candidates')
    primary = [unit for unit in evidence['units'] if unit['selection'] == 'primary']
    if len(primary) != 1:
        raise OwnershipLineageError('missing_ownership_primary')
    inventory_keys = ('filename', 'type', 'sequence', 'body_start', 'body_end', 'body_sha256')
    return _digest({**{key: evidence[key] for key in keys},
        'primary': {key: primary[0][key] for key in inventory_keys}})


def _check(check):
    if check is not None:
        check()


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _seal(value, key):
    return {**value, key: _digest(value)}


def _one(root, path, *, optional=False):
    nodes = root.findall(path)
    if optional and not nodes:
        return None
    if (len(nodes) != 1 or not (nodes[0].text or '').strip()
            or list(nodes[0]) or nodes[0].attrib or (nodes[0].tail or '').strip()):
        raise OwnershipLineageError('missing_or_ambiguous_ownership_field')
    return nodes[0].text.strip()


def _policy(evidence):
    return POLICY_V2 if evidence.get('supported_document_policy') == 'verified_supported_documents_v3' else POLICY


def _selected_unit(evidence, filename):
    units = [unit for unit in evidence['units'] if unit['filename'] == filename]
    inventory = [doc for doc in evidence['document_inventory'] if doc['filename'] == filename]
    keys = ('filename','type','sequence','body_start','body_end','body_sha256')
    if len(units) != 1 or len(inventory) != 1 or any(units[0][key] != inventory[0][key] for key in keys):
        raise OwnershipLineageError('invalid_incorporation_source_unit')
    unit = units[0]
    text = unit['text']
    if not isinstance(text,str) or unit['text_start'] != 0 or unit['text_end'] != len(text) or unit['text_sha256'] != hashlib.sha256(text.encode('utf-8')).hexdigest():
        raise OwnershipLineageError('invalid_incorporation_source_text')
    return unit


def _html_hrefs(evidence, unit, *, check=None):
    if _policy(evidence) != POLICY_V2 or not unit['filename'].lower().endswith(('.htm','.html')):
        return []
    from .filing_html_structural import validate_html_structural_receipt, iter_html_structural_tokens
    receipt = evidence.get('html_evidence', {}).get(unit['filename'])
    if not isinstance(receipt,dict):
        raise OwnershipLineageError('missing_incorporation_html_receipt')
    validate_html_structural_receipt(receipt,check=check)
    if (receipt['input_sha256'] != unit['body_sha256'] or receipt['input_bytes'] != unit['body_end']-unit['body_start']
            or receipt['source_text_sha256'] != unit['text_sha256']
            or receipt['source_text_chars'] != len(unit['text'])
            or receipt['source_text_utf8_bytes'] != len(unit['text'].encode('utf-8'))
            or receipt['source_text_use'] != 'incorporation_locator_only_not_analysis'):
        raise OwnershipLineageError('invalid_incorporation_html_binding')
    hrefs = []
    for token in iter_html_structural_tokens(receipt):
        _check(check)
        if token['kind'] in {'start','empty'} and token['tag']=='a' and 'href' in token['attributes']:
            hrefs.append(token['attributes']['href'])
    return hrefs


def _unit_binding(evidence, unit):
    receipt = evidence.get('html_evidence', {}).get(unit['filename'])
    return {'accession': evidence['accession'], **{key:unit[key] for key in ('filename','body_start','body_end','body_sha256','text_sha256')},
        'text_utf8_bytes':len(unit['text'].encode('utf-8')), 'text_char_start':0, 'text_char_end':len(unit['text']),
        'html_receipt_sha256':_digest(receipt) if receipt is not None else None}


def _unit_incorporations(evidence, unit, *, check=None):
    _check(check)
    if _policy(evidence) != POLICY_V2:
        return _incorporations(unit['text'], 'unit:' + unit['filename'])
    unit = _selected_unit(evidence, unit['filename'])
    hrefs = _html_hrefs(evidence, unit, check=check)
    return _incorporations(unit['text'], 'unit:' + unit['filename'],
        locator_text=unit['text']+'\n'+'\n'.join(hrefs), source_unit=_unit_binding(evidence,unit))


def resolve_incorporation_reference(reference, evidence, *, check=None):
    """Return the complete bound locator source; never analysis authority."""
    _check(check)
    if reference.get('reference_kind') == UNIT_REFERENCE:
        if _policy(evidence) != POLICY_V2 or 'quote' in reference or type(reference.get('source_unit')) is not dict:
            raise OwnershipLineageError('invalid_incorporation_reference_kind')
        binding = reference['source_unit']
        filename = binding.get('filename')
        if reference.get('source_path') != 'unit:' + str(filename):
            raise OwnershipLineageError('invalid_incorporation_source_path')
        unit = _selected_unit(evidence,filename)
        _html_hrefs(evidence,unit,check=check)
        if binding != _unit_binding(evidence,unit):
            raise OwnershipLineageError('invalid_incorporation_source_binding')
        if len(json.dumps(binding,sort_keys=True,ensure_ascii=True,separators=(',',':')).encode()) > MAX_INLINE_REFERENCE_BYTES:
            raise OwnershipLineageError('incorporation_reference_byte_limit')
        return unit['text']
    if 'reference_kind' in reference or 'source_unit' in reference:
        raise OwnershipLineageError('invalid_incorporation_reference_kind')
    text = reference.get('quote')
    if not isinstance(text,str) or len(text.encode('utf-8')) > MAX_INLINE_REFERENCE_BYTES:
        raise OwnershipLineageError('incorporation_reference_byte_limit')
    if _policy(evidence)==POLICY_V2 and str(reference.get('source_path','')).startswith('unit:'):
        unit = _selected_unit(evidence,reference['source_path'][5:])
        if text != unit['text']:
            raise OwnershipLineageError('invalid_incorporation_source_text')
        _html_hrefs(evidence,unit,check=check)
    return text


def resolve_incorporation_locators(reference, evidence, *, check=None):
    text = resolve_incorporation_reference(reference,evidence,check=check)
    if _policy(evidence)==POLICY_V2 and str(reference.get('source_path','')).startswith('unit:'):
        unit = _selected_unit(evidence,reference['source_path'][5:])
        return text+'\n'+'\n'.join(_html_hrefs(evidence,unit,check=check))
    if _policy(evidence)==POLICY_V2:
        import base64
        try:
            raw=base64.b64decode(evidence['ownership_primary_body'],validate=True)
            primary=[unit for unit in evidence['units'] if unit['selection']=='primary']
            if len(primary)!=1 or hashlib.sha256(raw).hexdigest()!=primary[0]['body_sha256']:
                raise OwnershipLineageError('invalid_incorporation_primary_body')
            xml=raw.strip()
            if xml.startswith(b'<XML>') and xml.endswith(b'</XML>'):xml=xml[5:-6].strip()
            if re.search(rb'<!\s*(?:DOCTYPE|ENTITY)\b',xml,re.I):
                raise OwnershipLineageError('unsafe_ownership_xml')
            root=ET.fromstring(xml)
            forms=[node for node in root if node.tag.split('}',1)[-1]=='formData']
            parts=reference.get('source_path','').split('/')
            if len(forms)!=1 or len(parts)<2 or parts[:2]!=['','formData'] or len(parts)>65 or any(not re.fullmatch('[A-Za-z][A-Za-z0-9]*',part) for part in parts[1:]):
                raise OwnershipLineageError('ambiguous_incorporation_xml_path')
            node=forms[0]
            for part in parts[2:]:
                _check(check)
                children=[child for child in node if child.tag.split('}',1)[-1]==part]
                if len(children)!=1:raise OwnershipLineageError('ambiguous_incorporation_xml_path')
                node=children[0]
            if list(node):raise OwnershipLineageError('invalid_incorporation_xml_leaf')
            raw_text=(node.text or '').strip()
            from .filing_evidence import _visible_text
            if _visible_text(raw_text.encode('utf-8'),check=check)!=text:
                raise OwnershipLineageError('invalid_incorporation_xml_projection')
            return raw_text
        except (KeyError,TypeError,ValueError,ET.ParseError) as error:
            if isinstance(error,OwnershipLineageError):raise
            raise OwnershipLineageError('invalid_incorporation_primary_body') from error
    return text


def _incorporations(text, path, *, locator_text=None, source_unit=None):
    if not (re.search(r'\bincorporat\w*', text, re.I)
            and re.search(r'\breference\b', text, re.I)):
        return []
    internal = re.fullmatch(r'(?:The )?information (?:contained |set forth )?(?:on|in) '
        r'(?:the )?(?:cover pages?|this Schedule 13[DG])(?: to this Schedule 13[DG])? '
        r'is incorporated (?:herein )?by reference\.?', text, re.I)
    if internal:
        return []
    if len(text.encode('utf-8')) > MAX_INLINE_REFERENCE_BYTES and source_unit is None:
        raise OwnershipLineageError('incorporation_reference_byte_limit')
    locators = text if locator_text is None else locator_text
    targets = set(_ACCESSION.findall(locators))
    documents = []
    for url in re.findall(r'https://www\.sec\.gov/Archives/edgar/data/[^\s<>"\']+', locators):
        parsed = urlsplit(url.rstrip('.,);'))
        match = re.fullmatch(r'/Archives/edgar/data/[1-9][0-9]{0,9}/([0-9]{18})/([A-Za-z0-9][A-Za-z0-9_.-]{0,254})', parsed.path)
        if match and not parsed.query and not parsed.fragment and '..' not in match[2]:
            compact = match[1]
            accession = compact[:10] + '-' + compact[10:12] + '-' + compact[12:]
            targets.add(accession)
            documents.append({'accession': accession, 'filename': match[2]})
    if source_unit is not None:
        documents = [{'accession':accession,'filename':filename} for accession,filename in sorted({(doc['accession'],doc['filename']) for doc in documents})]
    reference = {'source_path': path, 'target_accessions': sorted(targets),
        'target_documents': sorted(documents, key=lambda value: (value['accession'], value['filename']))}
    if len(text.encode('utf-8')) > MAX_INLINE_REFERENCE_BYTES:
        reference.update(reference_kind=UNIT_REFERENCE, source_unit=source_unit)
        if len(json.dumps(reference,sort_keys=True,ensure_ascii=True,separators=(',',':')).encode()) > MAX_INLINE_REFERENCE_BYTES:
            raise OwnershipLineageError('incorporation_reference_byte_limit')
    else:
        reference['quote'] = text
    return [reference]


def _primary_form_data(evidence):
    """Read only the exact retained native primary needed for local locators."""
    import base64
    try:
        raw=base64.b64decode(evidence['ownership_primary_body'],validate=True)
        primary=[unit for unit in evidence['units'] if unit['selection']=='primary']
        if len(primary)!=1 or hashlib.sha256(raw).hexdigest()!=primary[0]['body_sha256']:
            raise OwnershipLineageError('invalid_incorporation_primary_body')
        xml=raw.strip()
        if xml.startswith(b'<XML>') and xml.endswith(b'</XML>'):xml=xml[5:-6].strip()
        if re.search(rb'<!\s*(?:DOCTYPE|ENTITY)\b',xml,re.I):
            raise OwnershipLineageError('unsafe_ownership_xml')
        root=ET.fromstring(xml)
        namespace='{http://www.sec.gov/edgar/schedule13D}'
        forms=root.findall(namespace+'formData')
        if root.tag!=namespace+'edgarSubmission' or len(forms)!=1:
            raise OwnershipLineageError('unsupported_ownership_schema')
        return forms[0]
    except (KeyError,TypeError,ValueError,ET.ParseError) as error:
        if isinstance(error,OwnershipLineageError):raise
        raise OwnershipLineageError('invalid_incorporation_primary_body') from error


def _primary_incorporations(text,path,*,locator_text,evidence,form_data=None,check=None):
    """Resolve only wholly recognized same-original primary XML references.

    Every incorporation/reference token must belong to a finite explicit local
    clause. A missing target or even one unrecognized clause leaves the complete
    original reference unresolved. Selected exhibit references are independent.
    """
    references=_incorporations(text,path,locator_text=locator_text)
    allowed={'/formData/items1To7/item4/transactionPurpose',
        '/formData/items1To7/item5/percentageOfClassSecurities',
        '/formData/items1To7/item5/numberOfShares',
        '/formData/items1To7/item6/contractDescription'}
    if (_policy(evidence)!=POLICY_V2 or filing_form_family(evidence['form'])!='SCHEDULE 13D'
            or path not in allowed or not references or references[0]['target_accessions']
            or re.search(r'<\s*a\b|https?://|www\.',locator_text,re.I)):
        return references
    _check(check)
    try:
        form_data=_primary_form_data(evidence) if form_data is None else form_data
        namespace='{http://www.sec.gov/edgar/schedule13D}'
        if form_data.tag!=namespace+'formData':return references
        def unique(parts):
            node=form_data
            for part in parts:
                _check(check)
                children=node.findall(namespace+part)
                if len(children)!=1:raise OwnershipLineageError('ambiguous_local_incorporation_target')
                node=children[0]
            return node
        source=unique(path.split('/')[2:])
        if list(source) or (source.text or '').strip()!=locator_text:return references
        primary=[u for u in evidence['units'] if u['selection']=='primary']
        if len(primary)!=1:return references
        primary=_selected_unit(evidence,primary[0]['filename'])
        if primary['representation']!='ownership_form_data':return references
        destination=path.split('/')[3][4:]
        spans=[];files=set()
        def local_scope(value):
            if value.casefold()=='schedule 13d':return True
            cover=unique(['coverPageHeader'])
            number=_one(cover,namespace+'amendmentNo',optional=True)
            return evidence['form']=='SCHEDULE 13D/A' and value.casefold()=='amendment no. '+str(number)
        scope=r'(Schedule 13D|Amendment No\. [1-9][0-9]{0,2})'
        item_pattern=(r'\bThe information set forth in Item ([46]) of this '+scope+
            r'(?: regarding the Subscription Agreement)? is (?:hereby )?incorporated by reference into this Item ([456])\.')
        for match in re.finditer(item_pattern,text,re.I):
            if match[3]!=destination or not local_scope(match[2]):return references
            leaf={'4':'transactionPurpose','6':'contractDescription'}[match[1]]
            target=unique(['items1To7','item'+match[1]])
            value=_one(target,namespace+leaf)
            from .filing_evidence import _visible_text
            if not _visible_text(value.encode('utf-8'),check=check):return references
            files.add(primary['filename']);spans.append(match.span())
        cover_pattern=(r'\bThe information set forth in rows (\(7\) through \(10\)|\(11\) and \(13\)) '
            r'of the cover page of this '+scope+r' is incorporated by reference into this Item (5)\.')
        for match in re.finditer(cover_pattern,text,re.I):
            if match[3]!=destination or not local_scope(match[2]):return references
            people=unique(['reportingPersons']).findall(namespace+'reportingPersonInfo')
            if not people:return references
            fields=('soleVotingPower','sharedVotingPower','soleDispositivePower','sharedDispositivePower') if match[1].lower()=='(7) through (10)' else ('aggregateAmountOwned','percentOfClass')
            for person in people:
                _check(check)
                for field in fields:_one(person,namespace+field)
            files.add(primary['filename']);spans.append(match.span())
        exhibit_pattern=(r'\b(?:a copy of )?which is attached (?:hereto as Exhibit (99\.[1-9][0-9]?)'
            r'|as Exhibit (99\.[1-9][0-9]?) hereto) and is incorporated herein by reference\.')
        for match in re.finditer(exhibit_pattern,text,re.I):
            number=match[1] or match[2]
            matches=[doc for doc in evidence['document_inventory'] if doc['type'].upper()=='EX-'+number]
            if len(matches)!=1:return references
            unit=_selected_unit(evidence,matches[0]['filename'])
            if unit['selection']!='dependency':return references
            files.add(unit['filename']);spans.append(match.span())
        intro=re.compile(r'\bItem 6 of the Schedule 13D is hereby amended to incorporate the following at the end thereof:',re.I)
        for match in intro.finditer(text):
            if destination!='6' or evidence['form']!='SCHEDULE 13D/A':return references
            files.add(primary['filename']);spans.append(match.span())
        tokens=list(re.finditer(r'\b(?:incorporat\w*|referenc\w*)\b',text,re.I))
        if not files or not tokens or not all(any(start<=m.start() and m.end()<=end for start,end in spans) for m in tokens):
            return references
        accession=evidence['accession']
        return [{**references[0],'target_accessions':[accession],
            'target_documents':[{'accession':accession,'filename':name} for name in sorted(files)]}]
    except OwnershipLineageError:
        # Positive local proof is optional; absence stays an acquisition duty.
        return references


def ownership_facts(evidence, primary_body, *, check=None):
    """Extract roster/security/pointer facts from hash-bound native SEC XML.

    Keeps all reporter names, including reporters without CIKs. FILED-BY is only
    an acquisition index identity. The entire XML tree is inspected for explicit
    external incorporation references; unknown targets produce resolve duties.
    No assumption about amendment-number completeness is made.
    """
    _check(check)
    if evidence['structural_status'] != 'complete' or evidence['issues'] != []:
        raise OwnershipLineageError('incomplete_ownership_submission')
    if filing_form_family(evidence['form']) not in _FAMILIES:
        raise OwnershipLineageError('unsupported_ownership_form')
    primary = [u for u in evidence['units'] if u['selection'] == 'primary']
    if len(primary) != 1 or primary[0]['representation'] != 'ownership_form_data':
        raise OwnershipLineageError('unsupported_ownership_representation')
    if type(primary_body) is not bytes or hashlib.sha256(primary_body).hexdigest() != primary[0]['body_sha256']:
        raise OwnershipLineageError('ownership_primary_body_mismatch')
    raw = primary_body.strip()
    if raw.startswith(b'<XML>') and raw.endswith(b'</XML>'):
        raw = raw[5:-6].strip()
    xml = raw.decode('utf-8-sig')
    if '\x00' in xml or re.search(r'<!\s*(?:DOCTYPE|ENTITY)\b', xml, re.I):
        raise OwnershipLineageError('unsafe_ownership_xml')
    encoding = re.match(r"<\?xml\b[^?]*\bencoding\s*=\s*(['\"])([^'\"]+)\1", xml, re.I)
    if encoding and encoding[2].lower() not in {'utf-8', 'utf8', 'ascii', 'us-ascii'}:
        raise OwnershipLineageError('unsupported_ownership_encoding')
    root = ET.fromstring(xml)
    family = filing_form_family(evidence['form'])
    namespace = '{http://www.sec.gov/edgar/' + ('schedule13D' if family == 'SCHEDULE 13D' else 'schedule13g') + '}'
    if root.tag != namespace + 'edgarSubmission':
        raise OwnershipLineageError('unsupported_ownership_schema')
    nodes = list(root.iter())
    if len(nodes) > 100000 or any(not node.tag.startswith((namespace, '{http://www.sec.gov/edgar/common}')) for node in nodes):
        raise OwnershipLineageError('unsupported_ownership_schema')
    # The supported native schema represents prose/scalars as leaf text and
    # structure as element-only containers. Fragment-by-fragment projection of
    # mixed content can erase a CUSIP or split an incorporated-agreement duty.
    if any((list(node) and (node.text or '').strip())
            or (node.tail or '').strip() for node in nodes):
        raise OwnershipLineageError('unsupported_ownership_mixed_content')
    if len(root.findall(namespace + 'formData')) != 1 or len(root.findall(namespace + 'headerData')) != 1:
        raise OwnershipLineageError('invalid_ownership_structure')
    if normalize_filing_form(_one(root, namespace + 'headerData/' + namespace + 'submissionType')) != evidence['form']:
        raise OwnershipLineageError('ownership_form_mismatch')
    form_data = root.find(namespace + 'formData')
    if len(form_data.findall(namespace + 'coverPageHeader')) != 1:
        raise OwnershipLineageError('invalid_ownership_cover_structure')
    cover = form_data.find(namespace + 'coverPageHeader')
    family = filing_form_family(evidence['form'])
    subject = _cik(_one(cover, namespace + 'issuerInfo/' + namespace +
        ('issuerCIK' if family == 'SCHEDULE 13D' else 'issuerCik')))
    if {r['cik'] for r in evidence['issuer_candidates']} != {subject}:
        raise OwnershipLineageError('ownership_subject_mismatch')
    title = _one(cover, namespace + 'securitiesClassTitle')
    cusips = []
    # Only the supported native security paths may supply CUSIP facts. A
    # suggestively named descendant is not a security identifier authority.
    issuer = cover.find(namespace + 'issuerInfo')
    containers = [node for node in issuer if node.tag == namespace + 'issuerCusips']
    if len(containers) != 1 or containers[0].attrib or not list(containers[0]):
        raise OwnershipLineageError('unproven_ownership_securities')
    security_nodes = list(containers[0])
    allowed_cusip_nodes = {id(containers[0]), *(id(node) for node in security_nodes)}
    if any('cusip' in node.tag.split('}', 1)[-1].casefold()
            and id(node) not in allowed_cusip_nodes for node in cover.iter()):
        raise OwnershipLineageError('unsupported_ownership_security_structure')
    for node in security_nodes:
        _check(check)
        if node.tag != namespace + 'issuerCusipNumber' or list(node) or node.attrib:
            raise OwnershipLineageError('unsupported_ownership_security_structure')
        value = (node.text or '').strip()
        if value not in cusips:
            cusips.append(value)
    if not cusips or any(value != '' and not re.fullmatch(r'[A-Za-z0-9*@#]{9,12}', value) for value in cusips):
        raise OwnershipLineageError('unproven_ownership_securities')
    person_path = (namespace + 'reportingPersons/' + namespace + 'reportingPersonInfo'
        if family == 'SCHEDULE 13D' else namespace + 'coverPageHeaderReportingPersonDetails')
    people = form_data.findall(person_path)
    if not people or len(people) > 1000:
        raise OwnershipLineageError('unproven_reporting_roster')
    reporters = []
    for node in people:
        _check(check)
        name = _one(node, namespace + 'reportingPersonName')
        cik_tag = 'reportingPersonCIK' if family == 'SCHEDULE 13D' else 'reportingCik'
        cik = _one(node, namespace + cik_tag, optional=True)
        cik = _cik(cik) if cik else None
        no_cik_tag = 'reportingPersonNoCIK' if family == 'SCHEDULE 13D' else 'reportingCikNotPresentInEdgarFlag'
        no_cik = _one(node, namespace + no_cik_tag, optional=True)
        if no_cik == 'Y' and cik is not None:
            raise OwnershipLineageError('conflicting_reporting_cik')
        reporters.append({'name': name, 'cik': cik})
    if len({_digest(person) for person in reporters}) != len(reporters):
        raise OwnershipLineageError('duplicate_reporting_person')
    number = _one(cover, namespace + 'amendmentNo', optional=True)
    if number is not None and (not re.fullmatch(r'[0-9]{1,3}', number) or not 1 <= int(number) <= 999):
        raise OwnershipLineageError('invalid_amendment_number')
    pointers = root.findall(namespace + 'headerData/' + namespace + 'previousAccessionNumber')
    if len(pointers) > 1:
        raise OwnershipLineageError('ambiguous_previous_accession')
    if any(list(node) or node.attrib or (node.tail or '').strip() for node in pointers):
        raise OwnershipLineageError('invalid_previous_accession')
    previous = [(node.text or '').strip() for node in pointers]
    if any(not _ACCESSION.fullmatch(value) for value in previous):
        raise OwnershipLineageError('invalid_previous_accession')
    incorporations = []
    def visit(node, path):
        _check(check)
        local = node.tag.split('}', 1)[-1]
        if path.count('/') > 64:
            raise OwnershipLineageError('ownership_depth_limit')
        current_path = path + '/' + local
        raw_text = (node.text or '').strip()
        # Native XML prose can contain escaped HTML. Use the same visible
        # projection supplied to the model, while retaining original anchor
        # href/accession locators so formatting cannot hide an obligation.
        from .filing_evidence import _visible_text
        text = _visible_text(raw_text.encode('utf-8'), check=check) if raw_text else ''
        incorporations.extend(_primary_incorporations(text,current_path,locator_text=raw_text,
            evidence=evidence,form_data=form_data,check=check))
        for child in node:
            visit(child, current_path)
    visit(form_data, '')
    for unit in evidence['units']:
        if unit['selection'] != 'primary':
            incorporations.extend(_unit_incorporations(evidence,unit,check=check))
    value = {'policy': _policy(evidence), 'accession': evidence['accession'], 'form': evidence['form'],
        'family': family, 'is_amendment': evidence['form'].endswith('/A'),
        'filing_date': evidence['filing_date'], 'accepted_at': evidence['accepted_at'],
        'subject_cik': subject,
        'source_ciks': sorted({r['cik'] for r in evidence['roles'] if r['role'] == 'FILED-BY'}),
        'reporters': reporters, 'securities': {'class_title': title, 'cusips': sorted(cusips)},
        'amendment_number': int(number) if number is not None else None,
        'previous_accessions': previous, 'incorporations': incorporations,
        'source_identity_sha256': ownership_evidence_digest(evidence), 'primary_body_sha256': primary[0]['body_sha256']}
    return _seal(value, 'facts_sha256')


def validate_ownership_facts(facts, evidence, primary_body, *, check=None):
    if facts != ownership_facts(evidence, primary_body, check=check):
        raise OwnershipLineageError('ownership_facts_mismatch')
    return facts


def _facts(facts, evidence, *, check=None):
    if evidence['structural_status'] != 'complete' or evidence['issues'] != []:
        raise OwnershipLineageError('incomplete_ownership_submission')
    if (facts['policy'] != _policy(evidence) or facts['accession'] != evidence['accession']
            or facts['form'] != evidence['form'] or facts['filing_date'] != evidence['filing_date']
            or facts['accepted_at'] != evidence['accepted_at']
            or facts['subject_cik'] != evidence['issuer_candidates'][0]['cik']
            or facts['source_identity_sha256'] != ownership_evidence_digest(evidence)
            or facts['facts_sha256'] != _digest({k: v for k, v in facts.items() if k != 'facts_sha256'})):
        raise OwnershipLineageError('ownership_facts_mismatch')
    if _policy(evidence)==POLICY_V2:
        expected = [reference for unit in evidence['units'] if unit['selection']!='primary'
            for reference in _unit_incorporations(evidence,unit,check=check)]
        actual = [reference for reference in facts['incorporations'] if reference.get('source_path','').startswith('unit:')]
        if actual != expected:
            raise OwnershipLineageError('ownership_unit_references_mismatch')
    if not facts['reporters'] or not facts['securities']['cusips']:
        raise OwnershipLineageError('unproven_ownership_identity')
    return facts


def _identity(facts):
    return ({(r['cik'], ' '.join(r['name'].casefold().split())) for r in facts['reporters']},
            (facts['securities']['class_title'], tuple(facts['securities']['cusips'])))


def _related(left, right):
    if left['subject_cik'] != right['subject_cik'] or left['family'] != right['family']:
        return False
    def person_overlap(a, b):
        return ((a['cik'] is not None and a['cik'] == b['cik'])
                or ' '.join(a['name'].casefold().split()) == ' '.join(b['name'].casefold().split()))
    return (bool(set(left['securities']['cusips']) & set(right['securities']['cusips']))
            and any(person_overlap(a, b) for a in left['reporters'] for b in right['reporters']))


def ownership_lineage(current, facts_by_accession, submissions, histories, archives, *,
                      max_candidates=128, max_edges=256, check=None):
    """Select every necessary candidate before making a lineage disposition.

    A base needs no historical comparator. Amendments scan complete subject
    history plus all archives preceding the current date, and acquire
    every same-family candidate before ruling out unrelated identities. Exactly
    matching full rosters/securities and a unique base are required. Group/class
    changes, multiple bases, chronology ties and reference conflicts are retained
    as ambiguity only after every acquisition duty is closed. Missing material,
    incomplete responses and resource exhaustion never become ambiguity.

    Input facts are parser attestations, not independent durable authority. Use
    the raw-validating companion below when accepting a saved receipt.
    """
    from .filing_comparison_policy import checked_history, _rows
    from .fetch_errors import SourceFetchError
    _check(check)
    accession = current.get('accession') if isinstance(current, dict) else None
    receipt = {'policy': _policy(current), 'status': 'technical_failure', 'reason': 'invalid_lineage_input',
        'current_accession': accession, 'subject_cik': None, 'reporters': [], 'securities': {},
        'chain_accessions': [], 'candidate_accessions': [], 'source_accessions': [],
        'incorporated_accessions': [], 'obligations': []}
    proof = {'facts': {}, 'submissions': {}, 'histories': {}, 'archives': {}}
    obligations = []
    incorporated = set()
    def duty(kind, **fields):
        value = {'kind': kind, **fields}
        if value not in obligations:
            obligations.append(value)
        if len(obligations) > max_edges:
            raise OwnershipLineageError('lineage_edge_limit')
    def finish(status, reason):
        receipt.update(status=status, reason=reason, obligations=obligations,
                       source_accessions=sorted(proof['submissions']),
                       incorporated_accessions=sorted(incorporated), proof_sha256=_digest(proof))
        return receipt
    def acquire(candidate):
        _check(check)
        if candidate not in submissions:
            duty('complete_submission', accession=candidate)
            return None
        evidence = submissions[candidate]
        if 'failure' in evidence:
            raise OwnershipLineageError('ownership_submission_failure')
        if candidate not in facts_by_accession:
            duty('native_ownership_facts', accession=candidate)
            return None
        fact = _facts(facts_by_accession[candidate], evidence,check=check)
        if fact['accession'] != candidate:
            raise OwnershipLineageError('ownership_accession_mismatch')
        proof['facts'][candidate] = fact
        proof['submissions'][candidate] = _digest(evidence)
        return fact
    try:
        if type(max_candidates) is not int or not 1 <= max_candidates <= 128 or type(max_edges) is not int or not 1 <= max_edges <= 256:
            raise OwnershipLineageError('invalid_lineage_bounds')
        if submissions.get(accession) != current:
            raise OwnershipLineageError('current_submission_mismatch')
        present = acquire(accession)
        if present is None:
            return finish('acquisition_required', 'missing_current_facts')
        receipt.update(subject_cik=present['subject_cik'], reporters=present['reporters'],
                       securities=present['securities'])
        selected = {accession: present}
        candidates = {accession}
        # Metadata alone identifies candidates, never their reporter roster.
        if present['is_amendment']:
            if not present['source_ciks']:
                raise OwnershipLineageError('missing_source_filer_identity')
            # SEC entity history is the verified subject-indexed candidate
            # domain; FILED-BY remains source identity, not an unrelated-holdings
            # acquisition domain and never a substitute for the XML roster.
            ciks = [present['subject_cik']]
            metadata = {}
            found_current_in_subject_history = False
            for cik in ciks:
                _check(check)
                if cik not in histories:
                    duty('submission_history', cik=cik)
                    continue
                recent = checked_history(histories[cik], cik)
                proof['histories'][cik] = recent
                rows = list(recent['filings'])
                for descriptor in recent['archives']:
                    if descriptor['filingFrom'] > present['filing_date']:
                        continue
                    ref = cik + '/' + descriptor['name']
                    if ref not in archives:
                        duty('submission_archive', cik=cik, descriptor=descriptor)
                        continue
                    archive = archives[ref]
                    if ('failure' in archive or archive['cik'] != cik
                            or archive['coverage']['complete'] is not True
                            or archive['descriptor'] != descriptor):
                        raise OwnershipLineageError('incomplete_ownership_archive')
                    archive_rows = _rows(archive['filings'])
                    if len(archive_rows) != descriptor['filingCount'] or any(
                            not descriptor['filingFrom'] <= row['filing_date'] <= descriptor['filingTo']
                            for row in archive_rows):
                        raise OwnershipLineageError('ownership_archive_range_mismatch')
                    proof['archives'][ref] = archive
                    rows.extend(archive_rows)
                for row in rows:
                    if (filing_form_family(row['form']) == present['family']
                            and row['filing_date'] <= present['filing_date']):
                        key = row['accession_number']
                        if key in metadata and metadata[key] != row:
                            raise OwnershipLineageError('conflicting_ownership_history')
                        metadata[key] = row
                        candidates.add(key)
                        if key == accession:
                            if row['form'] != present['form'] or row['filing_date'] != present['filing_date']:
                                raise OwnershipLineageError('ownership_current_history_mismatch')
                            found_current_in_subject_history = True
                if len(candidates) > max_candidates:
                    raise OwnershipLineageError('lineage_candidate_limit')
            if not found_current_in_subject_history and not obligations:
                raise OwnershipLineageError('stale_ownership_history')
            for candidate in sorted(candidates - {accession}):
                fact = acquire(candidate)
                if fact is not None:
                    row = metadata[candidate]
                    if fact['form'] != normalize_filing_form(row['form']) or fact['filing_date'] != row['filing_date']:
                        raise OwnershipLineageError('ownership_history_identity_mismatch')
                    selected[candidate] = fact
        # Follow candidate references recursively with the same finite caps.
        def in_domain(fact):
            return (_related(present, fact) and (fact['accepted_at'] is None
                or present['accepted_at'] is None or fact['accepted_at'] <= present['accepted_at']))
        pending = [candidate for candidate, fact in selected.items() if in_domain(fact)]
        visited = set()
        conflicts = []
        edge_count = 0
        while pending:
            candidate = pending.pop()
            if candidate in visited:
                continue
            visited.add(candidate)
            fact = selected[candidate]
            edge_count += len(fact['previous_accessions']) + len(fact['incorporations'])
            if edge_count > max_edges:
                raise OwnershipLineageError('lineage_edge_limit')
            for target in fact['previous_accessions']:
                if target == candidate:
                    raise OwnershipLineageError('cyclic_ownership_reference')
                candidates.add(target)
                if len(candidates) > max_candidates:
                    raise OwnershipLineageError('lineage_candidate_limit')
                related = selected.get(target) or acquire(target)
                if related is not None:
                    selected[target] = related
                    if (related['filing_date'] > fact['filing_date'] or
                            (related['accepted_at'] is not None and fact['accepted_at'] is not None
                             and related['accepted_at'] > fact['accepted_at'])):
                        raise OwnershipLineageError('future_ownership_reference')
                    if in_domain(related):
                        pending.append(target)
                    if (related['subject_cik'] != fact['subject_cik']
                            or related['family'] != fact['family'] or _identity(related) != _identity(fact)):
                        conflicts.append('referenced_identity_conflict')
        # Incorporated submissions have their own source identity, not the
        # assessed subject's identity. Close nested explicit references too.
        reference_queue = [(candidate, reference) for candidate in visited
            for reference in selected[candidate]['incorporations']]
        seen_references = set()
        incorporated_edges = {}
        while reference_queue:
            _check(check)
            candidate, reference = reference_queue.pop()
            key = (candidate, _digest(reference))
            if key in seen_references:
                continue
            seen_references.add(key)
            edge_count += 1 + len(reference['target_accessions'])
            if edge_count > max_edges:
                raise OwnershipLineageError('lineage_edge_limit')
            source_evidence = submissions[candidate]
            reference_text = resolve_incorporation_reference(reference,source_evidence,check=check)
            if _policy(source_evidence)==POLICY_V2:
                locators = resolve_incorporation_locators(reference,source_evidence,check=check)
                binding = reference.get('source_unit')
                if reference['source_path'].startswith('unit:'):
                    binding = _unit_binding(source_evidence,_selected_unit(source_evidence,reference['source_path'][5:]))
                expected = (_incorporations(reference_text,reference['source_path'],locator_text=locators,source_unit=binding)
                    if reference['source_path'].startswith('unit:') else
                    _primary_incorporations(reference_text,reference['source_path'],locator_text=locators,
                        evidence=source_evidence,check=check))
                if expected != [reference]:
                    raise OwnershipLineageError('invalid_incorporation_reference_locators')
            targets = reference['target_accessions']
            if not targets:
                duty('resolve_incorporation', source_accession=candidate, **reference)
            for target in targets:
                incorporated_edges.setdefault(candidate, set()).add(target)
                if target != accession:
                    incorporated.add(target)
                if target not in submissions:
                    duty('incorporated_submission', accession=target,
                        source_accession=candidate, **reference,
                        required_exhibits=[doc['filename'] for doc in reference['target_documents']
                                          if doc['accession'] == target])
                    continue
                from .filing_assessment import _validate_evidence
                target_evidence = submissions[target]
                _validate_evidence(target_evidence, allow_unsupported_documents=True)
                target_files = {unit['filename'] for unit in target_evidence['units']}
                required_files = {doc['filename'] for doc in reference['target_documents']
                                  if doc['accession'] == target and doc['filename'] != target + '.txt'}
                if not required_files and re.search(r'agreement|contract|exhibit', reference_text, re.I):
                    required_files = {doc['filename'] for doc in target_evidence['document_inventory']
                                      if doc['type'].upper().startswith('EX-')}
                    if not required_files:
                        duty('resolve_incorporation', source_accession=candidate, **reference)
                for filename in sorted(required_files - target_files):
                    duty('incorporated_exhibit', accession=target, filename=filename,
                         source_accession=candidate, source_path=reference['source_path'])
                proof['submissions'][target] = _digest(target_evidence)
                if target_evidence['filing_date'] > present['filing_date']:
                    raise OwnershipLineageError('future_incorporated_submission')
                if target != candidate:
                    for unit in target_evidence['units']:
                        reference_queue.extend((target, nested) for nested in
                            _unit_incorporations(target_evidence,unit,check=check))
        completed_incorporations = set()
        def check_incorporation_cycles(candidate, active):
            _check(check)
            if candidate in active:
                raise OwnershipLineageError('cyclic_incorporation_reference')
            if candidate in completed_incorporations:
                return
            for target in incorporated_edges.get(candidate, ()):
                if target != candidate:
                    check_incorporation_cycles(target, active | {candidate})
            completed_incorporations.add(candidate)
        for candidate in incorporated_edges:
            check_incorporation_cycles(candidate, set())
        receipt['candidate_accessions'] = sorted(candidates)
        if obligations:
            return finish('acquisition_required', 'unclosed_ownership_obligations')
        # Detect pointer cycles even when traversal encountered an already-seen node.
        finished = set()
        def walk(candidate, active):
            _check(check)
            if candidate in active:
                raise OwnershipLineageError('cyclic_ownership_reference')
            if candidate in finished:
                return
            for target in selected[candidate]['previous_accessions']:
                if _related(present, selected[target]):
                    walk(target, active | {candidate})
            finished.add(candidate)
        for candidate, fact in selected.items():
            if in_domain(fact):
                walk(candidate, set())
        same = []
        identity = _identity(present)
        for candidate, fact in selected.items():
            _check(check)
            if not in_domain(fact):
                continue
            if fact['accepted_at'] is None:
                if not present['is_amendment'] and len(selected) == 1:
                    same.append(fact)
                    continue
                raise OwnershipLineageError('missing_ownership_acceptance')
            datetime.strptime(fact['accepted_at'], '%Y%m%d%H%M%S')
            if fact['accepted_at'][:8] > fact['filing_date'].replace('-', ''):
                raise OwnershipLineageError('ownership_acceptance_date_mismatch')
            if candidate != accession and fact['accepted_at'] >= present['accepted_at']:
                conflicts.append('unresolved_ownership_chronology')
            if fact['subject_cik'] != present['subject_cik']:
                continue
            other_identity = _identity(fact)
            if other_identity == identity:
                same.append(fact)
            elif _related(present, fact):
                conflicts.append('changed_reporting_group_or_security')
        bases = [fact for fact in same if not fact['is_amendment']]
        if len(bases) > 1:
            conflicts.append('multiple_candidate_bases')
        if present['is_amendment'] and not bases:
            conflicts.append('no_base_in_complete_candidate_corpus')
        if len({fact['accepted_at'] for fact in same}) != len(same):
            conflicts.append('unresolved_ownership_chronology')
        for fact in same:
            for target in fact['previous_accessions']:
                if selected[target]['accepted_at'] >= fact['accepted_at']:
                    conflicts.append('unresolved_ownership_chronology')
        if present['is_amendment']:
            if any('' in fact['securities']['cusips'] for fact in same):
                conflicts.append('unproven_security_identifiers')
            amendments = sorted((fact for fact in same if fact['is_amendment']),
                                key=lambda fact: fact['accepted_at'])
            # amendmentNo is optional. Present ordinals can contradict the
            # independently acquired corpus; absence alone proves no ambiguity.
            if any(fact['amendment_number'] is not None and fact['amendment_number'] != number
                   for number, fact in enumerate(amendments, 1)):
                conflicts.append('incomplete_declared_amendment_sequence')
        if conflicts:
            reason = ('referenced_identity_conflict' if 'referenced_identity_conflict' in conflicts
                      else sorted(set(conflicts))[0])
            return finish('ambiguous', reason)
        ordered = sorted(same, key=lambda fact: fact['accepted_at'] or '')
        if bases and ordered[0] != bases[0]:
            return finish('ambiguous', 'amendment_precedes_candidate_base')
        receipt['chain_accessions'] = [fact['accession'] for fact in ordered]
        return finish('complete', 'complete_ownership_candidate_corpus')
    except (KeyError, TypeError, AttributeError, ValueError, SourceFetchError) as error:
        reason = str(error) if isinstance(error, OwnershipLineageError) else 'invalid_or_incomplete_ownership_evidence'
        return finish('technical_failure', reason)


def validate_ownership_lineage(receipt, current, facts_by_accession, submissions, histories,
                               archives, primary_bodies, **limits):
    """Recompute all used raw facts and the durable receipt without source calls."""
    expected = ownership_lineage(current, facts_by_accession, submissions, histories, archives, **limits)
    # Validate the bounded used subgraph, including negative exclusion bodies.
    # Other independent graph filings must not enlarge this receipt's work.
    used = set(expected['candidate_accessions']) | {current['accession']}
    for accession in sorted(used):
        if accession not in facts_by_accession:
            continue  # The recomputed receipt retains its unclosed facts duty.
        try:
            validate_ownership_facts(facts_by_accession[accession], submissions[accession],
                primary_bodies[accession], check=limits.get('check'))
        except (KeyError, TypeError, ValueError) as error:
            raise OwnershipLineageError('invalid_ownership_lineage_raw_proof') from error
    if receipt != expected:
        raise OwnershipLineageError('invalid_ownership_lineage_receipt')
    return expected
