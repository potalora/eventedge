"""Pure, retained SEC inventory corroboration for a numeric count mismatch.

This proof accounts for every native document. It does not select documents,
establish text/material adequacy, or authorize any missing native body.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Comment

VERSION='sec-inventory-reconciliation-v1'
MAX_METADATA_BYTES=1024*1024
MAX_PROOF_BYTES=3*1024*1024
_BASE={'version','original','metadata'}
_BOUND={'native_header_sha256','declared_document_count','actual_document_count'}
_RECEIPT={'body','source_url','status_code','complete','size','sha256','observed_at'}
_DOCUMENT={'type','sequence','filename','body_start','body_end','body_sha256'}
_PARSER=None


def _invalid():raise ValueError('invalid_filing_inventory_reconciliation')


def _require(value):
    if not value:_invalid()


def _check(check):
    if check is not None:check()


def _digest(raw):return hashlib.sha256(raw).hexdigest()


def _hash(value):return isinstance(value,str) and re.fullmatch('[0-9a-f]{64}',value) is not None


def _time(value):
    _require(isinstance(value,str))
    try:parsed=datetime.fromisoformat(value)
    except (ValueError,TypeError):_invalid()
    _require(parsed.utcoffset() is not None and parsed<=datetime.now(timezone.utc))
    return parsed


def _parser():
    # Direct loading keeps the pure worker/replay boundary free of providers and
    # package initializers. These are the same native header/role rules.
    global _PARSER
    if _PARSER is None:
        spec=importlib.util.spec_from_file_location('_sec_inventory_native_rules',Path(__file__).with_name('filing_evidence.py'))
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);_PARSER=module
    return _PARSER


def _identity(identity):
    _require(isinstance(identity,dict) and set(identity)=={'accession','form','filing_date','source_url'})
    accession=identity['accession'];url=identity['source_url']
    _require(isinstance(accession,str) and re.fullmatch('[0-9]{10}-[0-9]{2}-[0-9]{6}',accession))
    _require(isinstance(url,str) and len(url)<=2048)
    parsed=urlsplit(url)
    directory=re.fullmatch(r'(/Archives/edgar/data/[1-9][0-9]{0,9}/'+accession.replace('-','')+')/'+accession+r'\.txt',parsed.path)
    _require(parsed.scheme=='https' and parsed.netloc=='www.sec.gov' and not parsed.query and not parsed.fragment
        and directory and parsed.geturl()==url)
    _require(isinstance(identity['form'],str) and _parser()._form(identity['form'])==identity['form'])
    try:_require(datetime.strptime(identity['filing_date'],'%Y-%m-%d').date().isoformat()==identity['filing_date'])
    except (ValueError,TypeError):_invalid()
    return 'https://www.sec.gov'+directory[1]


def _roles(roles):
    _require(isinstance(roles,list) and bool(roles))
    result=[]
    for row in roles:
        _require(isinstance(row,dict) and all(isinstance(row.get(k),str) and row[k] for k in ('role','cik','name')))
        result.append({k:row[k] for k in ('role','cik','name')})
    return result


def _fields_identity(values):
    parser=_parser();one=parser._one
    try:
        count=one(values,'PUBLIC-DOCUMENT-COUNT')
        _require(re.fullmatch('[0-9]+',count) is not None)
        return {'accession':one(values,'ACCESSION-NUMBER'),'form':parser._form(one(values,'TYPE')),
            'filing_date':datetime.strptime(one(values,'FILING-DATE'),'%Y%m%d').date().isoformat()},int(count)
    except (ValueError,TypeError):_invalid()


def _headers(body,check):
    parser=_parser();soup=BeautifulSoup(body,'html.parser');_check(check)
    comments=[str(c) for c in soup.find_all(string=lambda text:isinstance(text,Comment)) if '<SEC-HEADER>' in str(c)]
    pres=soup.find_all('pre')
    _require(len(comments)==1 and len(pres)==1)
    comment=comments[0].strip().encode('utf-8')
    _require(len(re.findall(rb'(?m)^<SEC-HEADER>',comment))==1 and len(re.findall(rb'(?m)^</SEC-HEADER>',comment))==1)
    comment_identity,declared=_fields_identity(parser._fields(comment))
    comment_roles=_roles(parser._nc_roles(comment))
    pre=pres[0].get_text().encode('utf-8');_check(check)
    starts=list(re.finditer(rb'(?m)^<DOCUMENT>[\r\n]',pre))
    _require(0<len(starts)<=2000)
    header=pre[:starts[0].start()]
    _require(re.fullmatch(rb'<SEC-DOCUMENT>'+comment_identity['accession'].encode()+rb'-index\.html : [0-9]{8}[\r\n].*',header,re.S) is not None)
    pre_identity,pre_count=_fields_identity(parser._direct_fields(header))
    _require(pre_identity==comment_identity and pre_count==declared and _roles(parser._direct_roles(header))==comment_roles)
    final=list(re.finditer(rb'(?m)^</SEC-DOCUMENT>',pre))
    _require(len(final)==1 and not pre[final[0].end():].strip())
    documents=[];names=set();sequences=set()
    for index,start in enumerate(starts):
        _check(check);end=starts[index+1].start() if index+1<len(starts) else final[0].start();block=pre[start.end():end]
        _require(len(re.findall(rb'(?m)^</DOCUMENT>',block))==1 and re.search(rb'</DOCUMENT>[\r\n]*\Z',block) is not None
            and len(re.findall(rb'(?m)^<TEXT>[\r\n]',block))==1)
        values=parser._fields(block.split(b'<TEXT>',1)[0]);kind=parser._one(values,'TYPE');seq=parser._one(values,'SEQUENCE');name=parser._one(values,'FILENAME')
        _require(re.fullmatch('[0-9]+',seq) is not None and int(seq)>0 and int(seq) not in sequences
            and re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.-]{0,254}',name) is not None and '..' not in name and name not in names)
        names.add(name);sequences.add(int(seq));documents.append({'type':kind,'sequence':int(seq),'filename':name})
    return comment_identity,declared,comment_roles,documents


def _unique(pairs):
    result={}
    for key,value in pairs:
        _require(key not in result);result[key]=value
    return result


def _projection(proof,check=None):
    _check(check)
    _require(isinstance(proof,dict) and set(proof) in (_BASE,_BASE|_BOUND) and proof['version']==VERSION)
    try:encoded=json.dumps(proof,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode('ascii')
    except (ValueError,TypeError,RecursionError):_invalid()
    _require(len(encoded)<=MAX_PROOF_BYTES)
    original=proof['original'];_require(isinstance(original,dict) and set(original)=={'identity','sha256','size','observed_at'})
    directory=_identity(original['identity']);origin_time=_time(original['observed_at'])
    _require(_hash(original['sha256']) and type(original['size']) is int and 0<original['size']<=512*1024**2)
    accession=original['identity']['accession'];metadata=proof['metadata']
    _require(isinstance(metadata,dict) and set(metadata)=={'index_headers','filing_index','directory'})
    suffixes={'index_headers':accession+'-index-headers.html','filing_index':accession+'-index.html','directory':'index.json'}
    for key,receipt in metadata.items():
        _check(check);_require(isinstance(receipt,dict) and set(receipt)==_RECEIPT and receipt['status_code']==200
            and type(receipt['status_code']) is int and receipt['complete'] is True and isinstance(receipt['body'],str))
        raw=receipt['body'].encode('utf-8')
        _require(type(receipt['size']) is int and 0<receipt['size']==len(raw)<=MAX_METADATA_BYTES
            and _hash(receipt['sha256']) and _digest(raw)==receipt['sha256']
            and receipt['source_url']==directory+'/'+suffixes[key] and _time(receipt['observed_at'])>=origin_time)
    identity,declared,roles,documents=_headers(metadata['index_headers']['body'],check)
    _require(identity=={k:original['identity'][k] for k in ('accession','form','filing_date')})
    _require(urlsplit(directory).path.split('/')[4].zfill(10) in {role['cik'] for role in roles})
    try:listing=json.loads(metadata['directory']['body'],object_pairs_hook=_unique,parse_constant=lambda _: _invalid())
    except (ValueError,TypeError,RecursionError):_invalid()
    _require(isinstance(listing,dict) and set(listing)=={'directory'} and isinstance(listing['directory'],dict))
    listing=listing['directory'];_require(listing.get('name')==urlsplit(directory).path and isinstance(listing.get('item'),list)
        and len(listing['item'])<=2003)
    names=[]
    for row in listing['item']:
        _check(check);_require(isinstance(row,dict) and isinstance(row.get('name'),str));name=row['name']
        _require(re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.-]{0,254}',name) is not None and '..' not in name and name not in names)
        names.append(name)
    own={accession+'-index.html',accession+'-index-headers.html',accession+'.txt'}
    _require(set(names)=={d['filename'] for d in documents}|own and len(names)==len(documents)+3)
    index=BeautifulSoup(metadata['filing_index']['body'],'html.parser');_check(check)
    rows=[]
    for row in index.find_all('tr'):
        cells=row.find_all('td',recursive=False)
        if any(cell.get_text(' ',strip=True)=='Complete submission text file' for cell in cells):rows.append(cells)
    _require(len(rows)==1 and len(rows[0])==5)
    cells=rows[0];links=cells[2].find_all('a')
    _require(len(links)==1 and links[0].get('href') in (original['identity']['source_url'],urlsplit(original['identity']['source_url']).path)
        and links[0].get_text(strip=True)==accession+'.txt')
    size=cells[4].get_text(strip=True);_require(re.fullmatch('[0-9]+',size) is not None and int(size)==original['size'])
    _check(check)
    return identity,declared,roles,documents


def build_reconciliation(metadata_responses, *, original_identity, original_sha256, original_size, observed_at):
    proof={'version':VERSION,'original':{'identity':deepcopy(original_identity),'sha256':original_sha256,
        'size':original_size,'observed_at':observed_at},'metadata':deepcopy(metadata_responses)}
    _projection(proof)
    return proof


def validate_reconciliation(proof, *, identity, submission_sha256, submission_size,
        document_inventory, declared_document_count, header_sha256, roles, observed_at, check=None):
    _require(isinstance(proof,dict) and set(proof)==_BASE|_BOUND)
    _,declared,independent_roles,documents=_projection(proof,check)
    _require(proof['original']=={'identity':identity,'sha256':submission_sha256,'size':submission_size,'observed_at':observed_at}
        and _hash(header_sha256) and proof['native_header_sha256']==header_sha256
        and type(declared_document_count) is int and proof['declared_document_count']==declared_document_count==declared
        and type(proof['actual_document_count']) is int and proof['actual_document_count']==len(documents)
        and declared!=len(documents) and _roles(roles)==independent_roles)
    _require(isinstance(document_inventory,list) and len(document_inventory)==len(documents))
    previous=0
    for native,independent in zip(document_inventory,documents):
        _check(check);_require(isinstance(native,dict) and set(native)==_DOCUMENT
            and {k:native[k] for k in ('type','sequence','filename')}==independent
            and type(native['sequence']) is int and _hash(native['body_sha256'])
            and type(native['body_start']) is int and type(native['body_end']) is int
            and previous<=native['body_start']<=native['body_end']<=submission_size)
        previous=native['body_end']


def validate_reconciled_evidence(evidence, *, original=None, check=None):
    if 'inventory_reconciliation' not in evidence:return
    proof=evidence['inventory_reconciliation'];_require('material_quarantine' not in evidence)
    _require(isinstance(proof,dict) and isinstance(proof.get('original'),dict))
    identity={'accession':evidence['accession'],'form':evidence['form'],'filing_date':evidence['filing_date'],
        'source_url':evidence.get('source_url',proof['original']['identity']['source_url'])}
    if original is not None:_require(proof['original']==original)
    validate_reconciliation(proof,identity=identity,submission_sha256=evidence['submission_sha256'],
        submission_size=proof['original']['size'],document_inventory=evidence['document_inventory'],
        declared_document_count=proof['declared_document_count'],header_sha256=evidence['header_sha256'],
        roles=evidence['roles'],observed_at=evidence['observed_at'],check=check)


def validate_evidence_reconciliation(evidence):
    """Revalidate retained current/prior evidence at the model-input boundary."""
    validate_reconciled_evidence(evidence)
