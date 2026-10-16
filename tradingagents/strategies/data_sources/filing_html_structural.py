"""Finite complete static HTML structure and same-original GRAPHIC transport.

This prospective verifier is deliberately not a browser, CSS layout engine or
image decoder. Unsupported syntax and incomplete closure fail; the complete
receipt supports deferral only. There is no extracted text/model authority.
"""
from __future__ import annotations
import binascii
import hashlib
import html.entities
import json
import re

VERSION = 'sec-html-structural-v2'
MAX_HTML_BYTES = 24 * 1024**2
MAX_RESOURCE_BYTES = 16 * 1024**2
MAX_INVENTORY_BYTES = 32 * 1024**2
_TAGS = frozenset('html head title meta body div p span a b strong i em u s strike del ins sup sub br hr pre blockquote center font small big h1 h2 h3 h4 h5 h6 ul ol li dl dt dd table thead tbody tfoot tr td th caption colgroup col img kbd'.split())
_VOID = frozenset({'br','hr','meta','img','col'})
_TABLE = frozenset({'table','thead','tbody','tfoot','tr','td','th','caption','colgroup','col'})
_INLINE = frozenset('span a b strong i em u s strike del ins sup sub br font small big img kbd'.split())
_COMMON = frozenset({'id','class','title','style','lang'})
_ATTRS = {'html': {'lang','version'}, 'meta': {'http-equiv','content','name','charset'},
    'a': {'href','name','target','rel'}, 'font': {'size','face','color'},
    'body': {'bgcolor','text','link','vlink','alink'}, 'p': {'align'}, 'div': {'align','border','cellpadding','cellspacing'},
    'table': {'width','height','border','cellpadding','cellspacing','align','bgcolor','summary','rules','frame'},
    'tr': {'align','valign','bgcolor','height'}, 'td': {'width','height','colspan','rowspan','align','valign','bgcolor','nowrap'},
    'th': {'width','height','colspan','rowspan','align','valign','bgcolor','nowrap','scope'},
    'col': {'width','span','align','valign'}, 'colgroup': {'width','span','align','valign'},
    'img': {'src','alt','width','height','border','align','hspace','vspace'},
    'hr': {'width','size','align','noshade','color'}, 'ol': {'start','type'}, 'ul': {'type'}, 'li': {'value','type'}}
_NAME = rb'[A-Za-z][A-Za-z0-9_.-]*(?::[A-Za-z][A-Za-z0-9_.-]*)?'
_ATTR = re.compile(rb'\s+(' + _NAME + rb')(?:\s*=\s*(?:"([^"<>]*)"|\x27([^\x27<>]*)\x27|([^\s"\x27`=<>]+)))?')
_NUMBER = r'(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)(?:[eE][+-]?[0-9]{1,2})?'
_LENGTH = r'(?:'+_NUMBER+r'(?:pt|px|pc|in|cm|mm|em|ex|%)?)'
_COLOR = r'(?:(?:#[0-9a-fA-F]{8}|#[0-9a-fA-F]{6}|#[0-9a-fA-F]{3})|#auto|black|whitesmoke|white|navajowhite|red|green|blue|gray|grey|silver|maroon|navy|purple|teal|aqua|lime|yellow|fuchsia|olive|orange|transparent|currentcolor|windowtext|rgb\(\s*(?:[0-9]{1,3}\s*,\s*){2}[0-9]{1,3}\s*\))'
_FONT_NAME = r'(?:[A-Za-z][A-Za-z0-9 -]*|"[A-Za-z\x27][A-Za-z0-9 ,\x27-]*"|\x27[A-Za-z"][A-Za-z0-9 ,"-]*\x27)'
_FONT = _FONT_NAME + r'(?:\s*,\s*' + _FONT_NAME + r')*'
_CSS_ENUMS = {'text-align':'left right center justify start end middle', 'vertical-align':'top middle bottom baseline sub super text-top text-bottom',
    'text-transform':'none uppercase lowercase capitalize','font-variant':'normal small-caps','font-style':'normal italic oblique','font-weight':'normal bold bolder lighter 100 200 300 400 500 600 700 800 900',
    'text-decoration':'none underline overline line-through underline solid',
    'display':'none block inline inline-block flex inline-flex table inline-table table-row table-cell table-row-group table-header-group table-footer-group table-column table-column-group table-caption',
    'visibility':'visible hidden collapse','font-kerning':'auto normal none',
    'align-items':'normal stretch center start end flex-start flex-end baseline',
    'justify-content':'normal center start end flex-start flex-end space-between space-around space-evenly',
    'align-content':'normal stretch center start end flex-start flex-end space-between space-around space-evenly',
    'box-sizing':'border-box content-box','flex-direction':'row row-reverse column column-reverse',
    'position':'static relative absolute fixed sticky','table-layout':'auto fixed',
    'word-break':'normal break-all keep-all break-word', 'border-collapse':'collapse separate',
    'white-space':'normal nowrap pre pre-wrap pre-line', 'break-before':'auto always page avoid', 'page-break-before':'auto always avoid left right',
    'page-break-after':'auto always avoid left right','page-break-inside':'auto avoid','list-style-type':'none disc circle square decimal lower-alpha upper-alpha lower-roman upper-roman',
    'clear':'none left right both', 'float':'none left right', 'overflow':'visible hidden clip scroll auto',
    'overflow-wrap':'normal break-word anywhere', 'text-align-last':'auto left right center justify start end',
    'text-decoration-line':'none underline overline line-through', 'text-decoration-style':'solid double dotted dashed wavy initial',
    'list-style-position':'inside outside','font-stretch':'normal condensed expanded',
    'font-size-adjust':'none','break-after':'auto always page column avoid',
    '-sec-extract':'summary exhibit', 'border-image':'none', 'cursor':'auto default pointer text none',
    'text-underline-style':'none single double thick dotted dash dot-dash dot-dot-dash wave',
    'text-justify-trim':'none punctuation punctuation-and-kana',
    'mso-bidi-font-weight':'normal bold', 'mso-spacerun':'yes no',
    'text-justify':'auto none inter-word inter-character inter-ideograph',
    'mso-yfti-firstrow':'yes no','mso-yfti-lastrow':'yes no',
    'writing-mode':'horizontal-tb vertical-rl vertical-lr', 'word-wrap':'normal break-word'}

_XHTML = 'http://www.w3.org/1999/xhtml'
_XML = 'http://www.w3.org/XML/1998/namespace'
_NAMESPACE_ROLES = {'http://www.xbrl.org/2013/inlineXBRL':'ix',
    'http://www.xbrl.org/2003/instance':'xbrli',
    'http://www.xbrl.org/2003/linkbase':'link',
    'http://xbrl.org/2006/xbrldi':'xbrldi',
    'http://www.w3.org/1999/xlink':'xlink',
    'http://www.w3.org/2001/XMLSchema-instance':'xsi', _XML:'xml',
    'http://xbrl.sec.gov/ecd/2026':'ecd', 'http://fasb.org/us-gaap/2026':'us-gaap',
    'http://xbrl.sec.gov/ecd/2025':'ecd2025'}
_TYPED_LEAVES = frozenset({'ecd:MeasureAxis.domain','us-gaap:QName.domain',
    'us-gaap:date.domain','ecd2025:RestatementDateAxis.domain'})
_XML_ATTRS = {
    'ix:header':set(), 'ix:hidden':set(), 'ix:references':{'target'}, 'ix:resources':set(),
    'ix:nonNumeric':{'name','contextRef','format','continuedAt','escape','target'},
    'ix:nonFraction':{'name','contextRef','unitRef','decimals','precision','scale','sign','format','target'},
    'ix:fraction':{'name','contextRef','unitRef','decimals','precision','target'},
    'ix:numerator':{'format','scale','sign'}, 'ix:denominator':{'format','scale','sign'},
    'ix:continuation':{'continuedAt'}, 'ix:exclude':set(),
    'ix:footnote':{'footnoteRole','footnoteID','continuedAt'},
    'ix:relationship':{'fromRefs','toRefs','arcrole','linkRole','order'},
    'xbrli:context':set(), 'xbrli:entity':set(), 'xbrli:identifier':{'scheme'},
    'xbrli:segment':set(), 'xbrli:scenario':set(), 'xbrli:period':set(),
    'xbrli:startDate':set(), 'xbrli:endDate':set(), 'xbrli:instant':set(),
    'xbrli:forever':set(), 'xbrli:unit':set(), 'xbrli:measure':set(),
    'xbrli:divide':set(), 'xbrli:unitNumerator':set(), 'xbrli:unitDenominator':set(),
    'xbrldi:explicitMember':{'dimension'}, 'xbrldi:typedMember':{'dimension'},
    'link:schemaRef':set(), **{leaf:set() for leaf in _TYPED_LEAVES}}
_PRESENTATION_ATTRS = frozenset('face size color bgcolor text link vlink alink align valign width height border cellpadding cellspacing rules frame nowrap noshade hspace vspace start type value version'.split())
_PRESENTATION_TAGS = frozenset('b strong i em u sup sub pre blockquote center font small big h1 h2 h3 h4 h5 h6 ul ol li dl dt dd hr kbd'.split())


class FilingHTMLStructuralError(ValueError):
    pass


def _fail(code):
    raise FilingHTMLStructuralError('html_' + code)


def _pulse(check):
    if check is not None:
        check()


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode('ascii')


def _entities(text):
    # Literal ampersands are allowed only when they cannot begin a reference.
    for match in re.finditer(r'&(?:#[xX][0-9a-fA-F]+;?|#[0-9]+;?|#[^;\s<&]*;?|[A-Za-z][A-Za-z0-9]*;?)', text):
        value = match.group()
        if not value.endswith(';'):
            _fail('ambiguous_entity')
        if value.startswith('&#'):
            digits = value[2:-1]
            try:
                number = int(digits[1:], 16) if digits.lower().startswith('x') else int(digits, 10)
            except ValueError:
                _fail('invalid_entity')
            if not 0 < number <= 0x10ffff or 0xd800 <= number <= 0xdfff or number < 32 and number not in (9,10,13):
                _fail('invalid_entity')
        elif value[1:] not in html.entities.html5:
            _fail('unknown_entity')
    import html as html_module
    return html_module.unescape(text)


def _css(value):
    # Only finite numeric rgb()/translate() grammars may contain parentheses. No URLs,
    # expressions, imports, variables or other executable/external CSS syntax.
    rgb = re.compile(r'rgb\(\s*(?:[0-9]{1,3}\s*,\s*){2}[0-9]{1,3}\s*\)',re.I)
    if any(any(int(n)>255 for n in re.findall(r'[0-9]+',m.group())) for m in rgb.finditer(value)):
        _fail('unsupported_css_value')
    translate=re.compile(r'translate\(\s*-?'+_LENGTH+r'\s*,\s*-?'+_LENGTH+r'\s*\)',re.I)
    rotate=re.compile(r'rotate\(\s*-?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)deg\s*\)',re.I)
    # Complete, nonrecursive static functions only. Preserve their spelling;
    # never evaluate arithmetic, clipping, visibility or used CSS values.
    calc=re.compile(r'calc\(\s*-?'+_LENGTH+r'[ \t\r\n]+[+-][ \t\r\n]+-?'+_LENGTH+r'\s*\)',re.I)
    coordinate=r'(?:auto|-?'+_LENGTH+r')'
    rect=re.compile(r'rect\(\s*'+coordinate+r'(?:\s*,\s*'+coordinate+r'){3}\s*\)',re.I)
    static=rotate.sub('ROTATE',rect.sub('RECT',calc.sub('CALC',translate.sub('TRANSLATE',rgb.sub('RGB',value)))))
    if len(value) > 16384 or re.search(r'[\\{}()@]|/\*|\*/|[\x00-\x08\x0b\x0c\x0e-\x1f]', static):
        _fail('unsupported_css_syntax')
    result = []
    declarations = value.split(';')
    for index, declaration in enumerate(declarations):
        if not declaration.strip(' \t\r\n'):
            # Empty declarations are valid separators, not error recovery.
            # Exact separators still belong to the retained style attribute.
            continue
        match = re.fullmatch(r'\s*([a-zA-Z-]+)\s*:\s*(.+?)\s*', declaration,re.S)
        if match is None:
            _fail('invalid_css_declaration')
        prop, val = match[1].lower(), match[2]
        important = bool(re.search(r'!\s*important\s*$', val, re.I))
        if important:
            val = re.sub(r'\s*!\s*important\s*$', '', val, flags=re.I).rstrip()
        if '!' in val:
            _fail('unsupported_css_syntax')
        # A CSS declaration list may repeat properties. Retain every occurrence
        # in source order, including !important; no cascade result is inferred.
        low = val.lower()
        parts = re.findall(_COLOR + r'|[^\s]+', low)
        border_styles={'none','hidden','solid','dashed','dotted','double','groove','ridge','inset','outset'}
        border_width=lambda p: p in {'thin','medium','thick'} or bool(re.fullmatch(_LENGTH,p))
        if prop in _CSS_ENUMS and prop != 'text-decoration':
            valid = low in _CSS_ENUMS[prop].split()
        elif prop in {'width','height','min-width','max-width','min-height','max-height','font-size','text-indent','letter-spacing','word-spacing','top','right','bottom','left','tab-size','-webkit-text-size-adjust'}:
            valid = bool(re.fullmatch(('-?' if prop in {'text-indent','top','right','bottom','left','letter-spacing','word-spacing'} else '') + _LENGTH, low)) or low in {'auto','normal','fit-content','max-content','min-content'} and prop not in {'font-size','text-indent'} or prop=='font-size' and low in {'smaller','larger','xx-small','x-small','small','medium','large','x-large','xx-large'} or calc.fullmatch(low) is not None
        elif prop=='clip':
            valid=low=='auto' or rect.fullmatch(low) is not None
        elif prop == 'z-index':
            # Integer stacking order is not a dimensional length. This changes
            # the earlier scalar grammar explicitly, retaining finite int32
            # syntax and exact tokens without computing any stacking result.
            valid = low=='auto' or bool(re.fullmatch(r'[+-]?[0-9]{1,10}',low)) and -2147483648<=int(low)<=2147483647
        elif prop == 'line-height':
            valid = low in {'normal','initial'} or bool(re.fullmatch(_LENGTH, low))
        elif prop in {'widows','orphans'}:
            valid = bool(re.fullmatch(r'[1-9][0-9]{0,3}',low))
        elif prop == '-sec-ix-hidden':
            valid = bool(re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.-]{0,255}',val))
        elif prop == 'transform':
            valid=(low=='none' or rotate.fullmatch(low) is not None
                or re.fullmatch(translate.pattern+r'(?:\s+'+rotate.pattern+r')?',low,re.I) is not None)
        elif prop=='rotate':
            valid=rotate.fullmatch('rotate('+low+')') is not None
        elif prop in {'flex-grow','flex-shrink'}:
            valid=re.fullmatch(_NUMBER,low) is not None
        elif prop=='column-count':
            valid=low=='auto' or bool(re.fullmatch(r'[1-9][0-9]{0,3}',low)) and int(low)<=1000
        elif prop=='column-gap':
            valid=low=='normal' or re.fullmatch(_LENGTH,low) is not None
        elif prop=='mso-yfti-irow':
            valid=re.fullmatch(r'[0-9]{1,4}',low) is not None
        elif prop == 'opacity':
            valid=bool(re.fullmatch(r'(?:0(?:\.[0-9]+)?|1(?:\.0+)?)',low))
        elif prop == 'border-spacing':
            valid = 1 <= len(low.split()) <= 2 and all(re.fullmatch(_LENGTH,p) for p in low.split())
        elif prop in {'margin-inline-start','margin-inline-end','margin-block-start','margin-block-end'}:
            valid = low == 'auto' or bool(re.fullmatch('-?' + _LENGTH,low))
        elif prop in {'margin','padding'} or prop in {a+'-'+b for a in ('margin','padding') for b in ('top','right','bottom','left')}:
            # Signed numeric padding is complete static syntax, including CSS
            # values a browser would ignore. Preserve it; do not assert CSS
            # property semantics, clamp it, or infer rendered visibility.
            parts = low.split(); valid = 1 <= len(parts) <= (4 if prop in {'margin','padding'} else 1) and all(re.fullmatch('-?' + _LENGTH, p) or p=='auto' and prop.startswith('margin') for p in parts)
        elif prop in {'color','background-color','background','text-decoration-color'}:
            valid = bool(re.fullmatch(_COLOR, low)) or prop=='text-decoration-color' and low=='initial'
        elif prop in {'border','border-top','border-right','border-bottom','border-left','mso-border-alt','mso-border-left-alt','mso-border-top-alt'}:
            valid = 1<=len(parts)<=3 and sum(border_width(p) for p in parts)<=1 and sum(p in border_styles for p in parts)<=1 and sum(bool(re.fullmatch(_COLOR,p)) for p in parts)<=1 and all(border_width(p) or p in border_styles or re.fullmatch(_COLOR,p) for p in parts)
        elif re.fullmatch(r'border(?:-(?:top|right|bottom|left))?-(?:width|style|color)',prop):
            maximum=4 if prop in {'border-width','border-style','border-color'} else 1
            valid=1<=len(parts)<=maximum and all(border_width(p) if prop.endswith('-width') else p in border_styles if prop.endswith('-style') else bool(re.fullmatch(_COLOR,p)) for p in parts)
        elif prop == 'text-decoration':
            valid = 1<=len(parts)<=3 and parts[0] in {'none','underline','overline','line-through'} and all(p in border_styles or re.fullmatch(_COLOR,p) for p in parts[1:])
        elif prop == 'text-decoration-thickness':
            valid=low in {'auto','from-font','initial'} or bool(re.fullmatch(_LENGTH,low))
        elif prop in {'font-family','mso-fareast-font-family'}:
            valid = bool(re.fullmatch(_FONT,val))
        elif prop == 'font':
            valid = bool(re.fullmatch(r'(?:(?:normal|italic|oblique|bold|small-caps|[1-9]00)\s+){0,3}' + _LENGTH + r'(?:/(?:normal|'+_LENGTH+r'))?\s+' + _FONT,val,re.I))
        else:
            _fail('unsupported_css_property')
        if not valid:
            _fail('unsupported_css_value')
        # Bound pathological numeric declarations; this is syntax, not layout.
        # The SEC hidden-fact reference is a bounded identifier, not a numeric
        # declaration. Digits in its opaque ID never assert numeric magnitude.
        # Hex colors are already checked by the finite property grammar. Their
        # exact digits can contain E (e.g. #215E99) without being a scientific
        # number. Exclude only complete accepted-size hex lexemes from this
        # derived magnitude scan; keep source values and dimensional bounds.
        numeric_value=re.sub(r'#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3})(?![A-Za-z0-9_.-])','',val)
        if prop not in {'-sec-ix-hidden','z-index'} and any(len(n)>32 or float(n)>1000000 for n in re.findall(_NUMBER,numeric_value)):
            _fail('css_numeric_limit')
        result.append({'property':prop,'value':val,'important':important})
    return result


def _role(tag, namespaces):
    """Resolve only finite structural namespace roles; never fetch a namespace."""
    if ':' not in tag:
        if tag not in _TAGS | {'xbrl','kbd'}:
            _fail('unsupported_tag')
        return tag
    prefix,local=tag.split(':')
    role=_NAMESPACE_ROLES.get(namespaces.get(prefix))
    canonical=role+':'+local if role else ''
    if canonical not in _XML_ATTRS:
        _fail('unsupported_namespace_element')
    return canonical


def _namespace_attributes(tag, attrs, namespaces):
    for key,value in attrs.items():
        if key=='xmlns' or key.startswith('xmlns:'):
            prefix='' if key=='xmlns' else key[6:]
            # One exact SEC resources profile declares a default instance URI
            # and clears it on the prefixed context. Every accepted descendant
            # is explicitly prefix-bound by the content model; unprefixed
            # instance/custom children fail. These metadata bindings need no
            # global mutation and cannot escape the subtree into later HTML.
            default_profile=(not prefix and tag!='html' and
                (_role(tag,namespaces)=='ix:resources' and value=='http://www.xbrl.org/2003/instance'
                 or _role(tag,namespaces)=='xbrli:context' and value==''))
            if default_profile:continue
            # A schemaRef is a leaf with no descendants. Its exact local XLink
            # declaration can be checked without introducing general XML
            # namespace rebinding or allowing the binding to escape the leaf.
            if tag!='html' and not (prefix and _role(tag,namespaces) in _XML_ATTRS and namespaces.get(prefix)==value
                    or prefix and _role(tag,namespaces)=='link:schemaRef'
                    and (value=='http://www.w3.org/1999/xlink'
                        or value=='http://www.xbrl.org/2003/linkbase' and namespaces.get(prefix)==value)):
                _fail('unsupported_namespace_scope')
            if prefix in {'xmlns','xml'} or not re.fullmatch(r'https?://[^\s<>"\x27]{1,2000}',value):
                _fail('invalid_namespace')
            if not prefix and value!=_XHTML:_fail('unsupported_default_namespace')
            namespaces[prefix]=value


def _attributes(tag, attrs, namespaces):
    """Common extraction/receipt checks, including namespace and navigation."""
    if tag!='html':namespaces=namespaces.copy()
    _namespace_attributes(tag,attrs,namespaces)
    role=_role(tag,namespaces)
    if role in _TYPED_LEAVES and attrs:_fail('unsupported_namespace_attribute')
    for key,value in attrs.items():
        if not isinstance(key,str) or not isinstance(value,str) or len(value)>16384:
            _fail('attribute_limit')
        if key=='xmlns' or key.startswith('xmlns:'):continue
        permitted=_COMMON | _ATTRS.get(role,set()) | _XML_ATTRS.get(role,set())
        if key not in permitted:
            if ':' not in key:_fail('unsupported_or_duplicate_attribute')
            prefix,local=key.split(':')
            ns=_NAMESPACE_ROLES.get(namespaces.get(prefix))
            if not (ns=='xml' and local in {'lang','space'}
                    or ns=='xsi' and local=='nil' and ':' in tag
                    or ns=='xlink' and role=='link:schemaRef' and local in {'type','href','arcrole'}):
                _fail('unsupported_namespace_attribute')
            if ns=='xsi' and value not in {'true','false','0','1'}:_fail('invalid_nil')
            if ns=='xlink' and local=='type' and value!='simple':_fail('unsupported_link_type')
            if ns=='xlink' and local=='arcrole' and value!='http://www.xbrl.org/2003/linkbase':_fail('unsupported_link_arcrole')
            if ns=='xml' and local=='space' and value not in {'preserve','default'}:_fail('invalid_xml_space')
        if key in {'rowspan','colspan','span'} and (not re.fullmatch(r'[1-9][0-9]{0,3}',value) or int(value)>1000):_fail('span_limit')
        if key=='lang' or key.endswith(':lang'):
            if not re.fullmatch(r'[A-Za-z]{1,8}(?:-[A-Za-z0-9]{1,8})*',value):_fail('invalid_language')
        if key in {'width','height','hspace','vspace','border','cellspacing','cellpadding','size'} and role!='font':
            suffix=r'(?:%|pt|px)?' if key in {'cellspacing','cellpadding'} else r'%?'
            if not re.fullmatch(r'[0-9]{1,6}(?:\.[0-9]{1,4})?'+suffix,value):_fail('invalid_presentation_number')
        if key=='version' and value!='-//XBRL International//DTD XHTML Inline XBRL 1.1//EN':_fail('unsupported_html_version')
        if key=='href' or key.endswith(':href'):
            # Browser URL parsing removes ASCII HT/LF/CR, including characters
            # supplied by references. Check that scheme while preserving the
            # exact decoded source attribute in the receipt.
            scheme_value=re.sub(r'[\t\r\n]','',value)
            if len(value)>4096 or re.match(r'\s*(?:javascript|data|vbscript):',scheme_value,re.I):_fail('unsafe_navigation')
        if key in {'name','dimension','format'} and role.startswith(('ix:','xbrldi:')):
            if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.-]*:[A-Za-z_][A-Za-z0-9_.-]*',value) or value.split(':')[0] not in namespaces:
                _fail('unbound_qname')
    _meta(role,attrs)
    return role


def _obligations(role, attrs, declarations):
    result=set()
    if role in _TABLE:result.add('table_layout')
    if role in {'strike','s','del','ins'}:result.add('redline')
    if declarations:result.add('css_declarations')
    if role in _PRESENTATION_TAGS:result.add('semantic_presentation')
    if any(k.endswith(':space') for k in attrs):result.add('semantic_presentation')
    if set(attrs)&_PRESENTATION_ATTRS:result.add('presentation_attributes')
    if role=='font' and 'face' in attrs:result.add('font_selection')
    if ':' in role or role=='xbrl':result.add('inline_xbrl')
    if role=='img':result.add('image_resource')
    return result


def _nest(tag, stack, namespaces=None):
    namespaces=namespaces or {}
    tag=_role(tag,namespaces);stack=[_role(t,namespaces) for t in stack]
    parent = stack[-1] if stack else None
    if tag=='xbrl':
        if stack:_fail('invalid_sec_wrapper')
        return
    if parent=='xbrl':
        if tag!='html':_fail('invalid_sec_wrapper')
        return
    xml_parents={'ix:hidden':{'ix:header'},'ix:references':{'ix:header'},'ix:resources':{'ix:header'},
        'link:schemaRef':{'ix:references'},'xbrli:context':{'ix:resources'},'xbrli:unit':{'ix:resources'},
        'xbrli:entity':{'xbrli:context'},'xbrli:identifier':{'xbrli:entity'},'xbrli:segment':{'xbrli:entity'},
        'xbrli:scenario':{'xbrli:context'},'xbrli:period':{'xbrli:context'},
        'xbrli:startDate':{'xbrli:period'},'xbrli:endDate':{'xbrli:period'},'xbrli:instant':{'xbrli:period'},'xbrli:forever':{'xbrli:period'},
        'xbrli:divide':{'xbrli:unit'},'xbrli:measure':{'xbrli:unit','xbrli:unitNumerator','xbrli:unitDenominator'},
        'xbrli:unitNumerator':{'xbrli:divide'},'xbrli:unitDenominator':{'xbrli:divide'},
        'xbrldi:explicitMember':{'xbrli:segment','xbrli:scenario'},'xbrldi:typedMember':{'xbrli:segment','xbrli:scenario'},
        'ix:numerator':{'ix:fraction'},'ix:denominator':{'ix:fraction'},
        **{leaf:{'xbrldi:typedMember'} for leaf in _TYPED_LEAVES}}
    if tag in xml_parents and parent not in xml_parents[tag]:_fail('invalid_namespace_content_model')
    if parent in set().union(*xml_parents.values()) | {'ix:hidden'}:
        allowed={t for t,parents in xml_parents.items() if parent in parents}
        if parent=='ix:hidden':allowed|={'ix:nonNumeric','ix:nonFraction','ix:fraction'}
        if parent=='ix:resources':allowed|={'ix:relationship'}
        if tag not in allowed:_fail('invalid_namespace_content_model')
    if parent in {'xbrli:identifier','xbrli:startDate','xbrli:endDate','xbrli:instant','xbrli:measure','xbrldi:explicitMember','link:schemaRef','ix:relationship','xbrli:forever'} | _TYPED_LEAVES:
        _fail('invalid_namespace_content_model')
    if tag=='ix:header' and parent not in {'body','div'}:_fail('invalid_namespace_content_model')
    if ':' in tag:
        if parent in {'html','head','table','thead','tbody','tfoot','tr','colgroup','ul','ol','dl'}:_fail('invalid_namespace_content_model')
        return
    parents = {'head':{'html'},'body':{'html'},'title':{'head'},'meta':{'head'},
        'thead':{'table'},'tbody':{'table'},'tfoot':{'table'},'caption':{'table'},
        'tr':{'table','thead','tbody','tfoot'},'td':{'tr'},'th':{'tr'},'colgroup':{'table'},'col':{'table','colgroup'},
        'li':{'ul','ol'},'dt':{'dl'},'dd':{'dl'}}
    if tag in parents and parent not in parents[tag]:
        _fail('invalid_content_model')
    if parent in {'table','thead','tbody','tfoot','tr','colgroup','ul','ol','dl','head','title'}:
        allowed = {'table':{'thead','tbody','tfoot','caption','tr','colgroup','col'},'thead':{'tr'},'tbody':{'tr'},'tfoot':{'tr'},'tr':{'td','th'},'colgroup':{'col'},'ul':{'li'},'ol':{'li'},'dl':{'dt','dd'},'head':{'title','meta'},'title':set()}[parent]
        if tag not in allowed:
            _fail('invalid_content_model')
    if (parent == 'p' or parent in _INLINE - _VOID) and tag not in _INLINE:
        _fail('invalid_content_model')
    if tag in {'a','p','html','head','body'} and tag in stack:
        _fail('invalid_nested_element')


def _document_start(tag, stack, state):
    if state.get('xbrl') and not stack and tag!='xbrl':_fail('outside_sec_wrapper')
    if tag=='xbrl':
        if stack or state.get('xbrl') or state['html']:_fail('invalid_sec_wrapper')
        state['xbrl']=True
    if tag == 'html':
        if stack not in ([],['xbrl']) or state['html']:
            _fail('duplicate_or_nested_html')
        state['html'] = True
    elif tag in {'head', 'body'}:
        if state[tag] or tag == 'head' and state['body']:
            _fail('duplicate_or_misordered_wrapper')
        state[tag] = True
    if stack and stack[-1] == 'html' and tag not in {'head', 'body'}:
        _fail('unsupported_implicit_body')


def _meta(tag, attrs):
    if tag != 'meta':
        return
    equiv = attrs.get('http-equiv', '').lower()
    if equiv and equiv not in {'content-type', 'content-style-type'}:
        _fail('active_meta')
    if 'charset' in attrs and attrs['charset'].lower() not in {'utf-8','utf8','us-ascii'}:
        _fail('unsupported_charset')
    if equiv == 'content-type' and not re.fullmatch(r'text/html(?:\s*;\s*(?:charset\s*=\s*(?:utf-8|utf8|us-ascii))?)?', attrs.get('content', ''), re.I):
        _fail('unsupported_charset')
    if equiv == 'content-style-type' and attrs.get('content', '').lower() != 'text/css':
        _fail('unsupported_style_type')


def _typed_member_inventory(receipt, namespaces, check):
    """One explicitly known text leaf, never arbitrary typed XML or type proof."""
    if 'http://xbrl.org/2006/xbrldi' not in namespaces.values():return
    stack=[];children=None
    for token in iter_html_structural_tokens(receipt):
        _pulse(check)
        kind=token['kind']
        if token.get('implicit_closes'):stack.pop()
        if kind in {'start','empty'}:
            role=_role(token['tag'],namespaces)
            if role=='xbrldi:typedMember':
                if kind=='empty' or children is not None:_fail('invalid_typed_member_inventory')
                children=0
            elif stack and stack[-1]=='xbrldi:typedMember':
                if role not in _TYPED_LEAVES:_fail('invalid_typed_member_inventory')
                children+=1
            if kind=='start' and token['tag'] not in _VOID:stack.append(role)
        elif kind=='end':
            role=_role(token['tag'],namespaces)
            if role=='xbrldi:typedMember':
                if children!=1:_fail('invalid_typed_member_inventory')
                children=None
            stack.pop()
        elif kind=='text' and stack and stack[-1]=='xbrldi:typedMember' and not token['whitespace_only']:
            _fail('invalid_typed_member_inventory')


def _transport(document, body_reader, limit, check):
    _pulse(check)
    if document['type'].upper() != 'GRAPHIC':
        _fail('resource_type')
    size = document['body_end']-document['body_start']
    if not 0 < size <= limit:
        _fail('resource_byte_limit')
    body = body_reader(document)
    if not isinstance(body,bytes) or len(body)!=size or _sha(body)!=document['body_sha256']:
        _fail('resource_integrity')
    lines = body.strip(b'\r\n\t ').splitlines()
    if len(lines)<4 or lines[0] != b'begin 644 '+document['filename'].encode('ascii') or lines[-1]!=b'end' or lines[-2] not in (b'`',b' ',b''):
        _fail('resource_uu_framing')
    decoded = bytearray(); trimmed = ignored = 0
    for line in lines[1:-2]:
        _pulse(check)
        if not line or not 33<=line[0]<=77 or any(not 32<=byte<=96 for byte in line):
            _fail('resource_uu_line')
        count = line[0]-32; expected = 1+4*((count+2)//3)
        if len(line)>expected:
            _fail('resource_uu_line')
        trimmed += expected-len(line)
        padded = line.ljust(expected,b' ')
        unused={0:0,1:2,2:1}[count%3]
        if unused:
            ignored += sum(byte not in (32,96) for byte in padded[-unused:])
            padded=padded[:-unused]+b' '*unused
        try:
            block=binascii.a2b_uu(padded)
        except binascii.Error:
            _fail('resource_uu_line')
        if len(block)!=count or len(decoded)+count>limit:
            _fail('resource_decoded_byte_limit')
        decoded.extend(block)
    if not decoded:
        _fail('resource_empty')
    # Encoded image payload validity is outside the explicitly approved proof.
    # Bind every transport byte and declared codec; never claim valid pixels.
    suffix=document['filename'].rsplit('.',1)[-1].lower()
    if suffix not in {'jpg','jpeg','gif','png'}:
        _fail('unsupported_resource_codec')
    return {**{k:document[k] for k in ('type','sequence','filename','body_start','body_end','body_sha256')},
        'transport':'sec_graphic_uu_v1','decoded_bytes':len(decoded),'decoded_sha256':_sha(decoded),
        'trimmed_zero_sextets':trimmed,'ignored_padding_sextets':ignored,
        'declared_codec':suffix,'encoded_image_validity':'unverified'}


def extract_html_structural_evidence(body, *, documents=(), body_reader=None,
        max_html_bytes=MAX_HTML_BYTES, max_resource_bytes=MAX_RESOURCE_BYTES,
        max_inventory_bytes=MAX_INVENTORY_BYTES, check=None):
    """Account for every source byte and resource edge without rendering/recovery."""
    _pulse(check)
    if not isinstance(body,bytes) or not 0<len(body)<=max_html_bytes:
        _fail('byte_limit')
    try:
        text=body.decode('utf-8')
    except UnicodeDecodeError:
        _fail('invalid_utf8')
    if any(ord(c)<32 and c not in '\t\r\n' for c in text):
        _fail('invalid_control')
    del text
    tokens=[];stack=[];edges=[];obligations=set();pos=0;inventory=0;doctype=False;p_content_start=None
    hashes=[];hash_ids={};attribute_sets=[];attribute_ids={};document_state={'html':False,'head':False,'body':False,'xml':False}
    namespaces={'xml':_XML}
    def append(token):
        nonlocal inventory
        digest=token['sha256']
        if digest not in hash_ids:
            hash_ids[digest]=len(hashes);hashes.append(digest);inventory+=len(_json(digest))+1
        kind=token['kind']
        row=[kind,token['start'],token['end'],hash_ids[digest]]
        if kind in {'start','empty'}:
            attributes={'attributes':token['attributes'],'css':token['css']}
            identity=_json(attributes)
            if identity not in attribute_ids:
                attribute_ids[identity]=len(attribute_sets);attribute_sets.append(attributes);inventory+=len(identity)+1
            row.extend([token['tag'],attribute_ids[identity],bool(token['implicit_closes'])])
        elif kind=='end':row.extend([token['tag'],bool(token['implicit_closes'])])
        elif kind=='text':row.append(token['whitespace_only'])
        inventory+=len(_json(row))+1
        if inventory>max_inventory_bytes:_fail('inventory_limit')
        tokens.append(row)
    while pos<len(body):
        _pulse(check)
        start=pos
        if body[pos:pos+4]==b'<!--':
            end=body.find(b'-->',pos+4)
            if end<0 or b'--' in body[pos+4:end] or re.search(rb'\[\s*(?:if|endif)\b',body[pos+4:end],re.I):
                _fail('invalid_comment')
            pos=end+3;token={'kind':'comment'}
        elif body[pos:pos+2]==b'<?':
            match=re.compile(rb'<\?xml\s+version\s*=\s*(["\x27])1\.0\1(?:\s+encoding\s*=\s*(["\x27])(UTF-8|utf-8|ASCII|US-ASCII)\2)?\s*\?>').match(body,pos)
            if match is None or stack!=['xbrl'] or document_state['html'] or document_state['xml']:
                _fail('unsupported_xml_declaration')
            if match[3] in (b'ASCII',b'US-ASCII') and not body.isascii():_fail('declared_encoding_mismatch')
            document_state['xml']=True;pos=match.end();token={'kind':'xml_declaration'}
        elif body[pos:pos+2]==b'<!':
            match=re.compile(rb'<!DOCTYPE\s+html\s*>',re.I).match(body,pos)
            if match is None or doctype or stack or any(t[0]!='text' or not t[4] for t in tokens):
                _fail('unsupported_declaration')
            doctype=True;pos=match.end();token={'kind':'doctype'}
        elif body[pos:pos+1]==b'<':
            # Quotes delimit complete attributes; a bare '<' is never recovered.
            match=re.compile(rb'<(/?)('+_NAME+rb')((?:[^<>"\x27]|"[^"<>]*"|\x27[^\x27<>]*\x27)*)>').match(body,pos)
            if match is None:
                _fail('invalid_token')
            closing=bool(match[1]);lexical=match[2].decode();tag=lexical if ':' in lexical else lexical.lower();tail=match[3];pos=match.end()
            if tag=='xbrl' and lexical!='XBRL':_fail('invalid_sec_wrapper')
            if tag=='xbrl' and not closing and any(t[0]=='text' and not t[4] for t in tokens):_fail('outside_sec_wrapper_text')
            if document_state['xml'] and ':' not in tag and tag!='xbrl' and lexical!=tag:_fail('xml_name_case')
            if closing or tag!='html':_role(tag,namespaces)
            implicit=[]
            if stack and stack[-1]=='p' and (closing and len(stack)>1 and tag==stack[-2]
                    or not closing and tag in {'p','div','hr','table','ul','ol','dl','blockquote','center','h1','h2','h3','h4','h5','h6'}):
                if document_state['xml'] or p_content_start is None or body[p_content_start:start].strip():
                    _fail('unsupported_paragraph_omission')
                stack.pop();implicit=['p'];p_content_start=None
            if closing:
                if tail.strip() or tag in _VOID or not stack or stack.pop()!=tag:
                    _fail('mismatched_close')
                token={'kind':'end','tag':tag,'implicit_closes':implicit}
            else:
                selfclose=tail.rstrip().endswith(b'/')
                if selfclose:tail=tail.rstrip()[:-1]
                if document_state['xml'] and tag in _VOID and not selfclose:_fail('xml_nonempty_void')
                if selfclose and tag not in _VOID and not (document_state['xml'] and namespaces.get('')==_XHTML):
                    _fail('nonvoid_self_close')
                attrs={};declarations=[];offset=0
                while offset<len(tail):
                    if not tail[offset:].strip():break
                    attr=_ATTR.match(tail,offset)
                    if attr is None:
                        _fail('invalid_attribute')
                    lexical_key=attr[1].decode()
                    key=lexical_key if ':' in lexical_key or ':' in tag or document_state['xml'] else lexical_key.lower()
                    if key in attrs:
                        _fail('unsupported_or_duplicate_attribute')
                    raw=next((v for v in attr.groups()[1:] if v is not None),None)
                    if document_state['xml'] and (raw is None or attr[4] is not None):_fail('xml_unquoted_attribute')
                    if raw is None and key not in {'nowrap','noshade'}:
                        _fail('missing_attribute_value')
                    value=_entities(raw.decode('utf-8')) if raw is not None else ''
                    if document_state['xml'] and raw is not None and re.search(rb'&(?!(?:amp|lt|gt|quot|apos|#[0-9]+|#x[0-9a-fA-F]+);)',raw):_fail('xml_entity')
                    if len(value)>16384:
                        _fail('attribute_limit')
                    if key=='style':declarations=_css(value)
                    if key in {'rowspan','colspan','span'} and (not re.fullmatch(r'[1-9][0-9]{0,3}',value) or int(value)>1000):
                        _fail('span_limit')
                    if tag=='meta' and key=='http-equiv' and value.lower() not in {'content-type','content-style-type'}:
                        _fail('active_meta')
                    if tag=='meta' and key=='charset' and value.lower() not in {'utf-8','utf8','us-ascii'}:
                        _fail('unsupported_charset')
                    if key=='lang' and not re.fullmatch(r'[A-Za-z]{1,8}(?:-[A-Za-z0-9]{1,8})*',value):
                        _fail('invalid_language')
                    if key=='href' and (len(value)>4096 or re.match(r'\s*(?:javascript|data|vbscript):',value,re.I)):
                        _fail('unsafe_navigation')
                    attrs[key]=value;offset=attr.end()
                role=_attributes(tag,attrs,namespaces)
                if tag=='html' and document_state['xml'] and namespaces.get('')!=_XHTML:_fail('missing_xhtml_namespace')
                _document_start(tag,stack,document_state)
                _nest(tag,stack,namespaces)
                if len(stack)>=64:_fail('depth_limit')
                if tag not in _VOID and not selfclose:stack.append(tag)
                if tag=='p':p_content_start=pos
                obligations.update(_obligations(role,attrs,declarations))
                token={'kind':'empty' if selfclose and tag not in _VOID else 'start','tag':tag,'attributes':attrs,'css':declarations,'implicit_closes':implicit}
                if tag=='img':
                    name=attrs.get('src','')
                    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,254}\.(?:jpe?g|png|gif)',name,re.I) or '..' in name:
                        _fail('unsafe_resource_reference')
                    edges.append({'token_index':len(tokens),'attribute':'src','filename':name})
                    obligations.add('image_resource')
        else:
            end=body.find(b'<',pos)
            pos=len(body) if end<0 else end
            value=_entities(body[start:pos].decode('utf-8'))
            if document_state.get('xbrl') and not stack and body[start:pos].strip():_fail('outside_sec_wrapper_text')
            if document_state['xml'] and (b']]>' in body[start:pos] or re.search(rb'&(?!(?:amp|lt|gt|quot|apos|#[0-9]+|#x[0-9a-fA-F]+);)',body[start:pos])):_fail('xml_entity')
            if stack and _role(stack[-1],namespaces) in {'xbrl','html','table','thead','tbody','tfoot','tr','colgroup','head','ul','ol','dl','ix:header','ix:hidden','ix:references','ix:resources','xbrli:context','xbrli:entity','xbrli:segment','xbrli:scenario','xbrli:period','xbrli:unit','xbrli:divide','xbrli:unitNumerator','xbrli:unitDenominator'} and value.strip():
                _fail('invalid_content_text')
            token={'kind':'text','whitespace_only':not body[start:pos].strip()}
        append({**token,'start':start,'end':pos,'sha256':_sha(body[start:pos])})
    if stack:_fail('unclosed_element')
    if document_state.get('xbrl') and not document_state['html']:_fail('missing_sec_html')
    names=sorted({edge['filename'] for edge in edges});resources=[]
    by_name={}
    for document in documents:
        _pulse(check)
        by_name.setdefault(document['filename'],[]).append(document)
    for name in names:
        _pulse(check)
        if len(by_name.get(name,[]))!=1 or body_reader is None:_fail('missing_or_ambiguous_resource')
        resource=_transport(by_name[name][0],body_reader,max_resource_bytes,check)
        inventory+=len(_json(resource))+1
        if inventory>max_inventory_bytes:_fail('inventory_limit')
        resources.append(resource)
    receipt={'version':VERSION,'input_sha256':_sha(body),'input_bytes':len(body),'tokens':tokens,'token_hashes':hashes,'attribute_sets':attribute_sets,
        'resource_edges':edges,'resources':resources,'resource_decoded_bytes':sum(r['decoded_bytes'] for r in resources),
        'obligations':sorted(obligations),'visual_presence':bool(obligations),'native_text_complete':False,
        'css_layout':'unverified','css_property_semantics':'unverified','font_availability':'unverified','encoded_image_validity':'unverified',
        'text_visibility':'unverified','visual_meaning':'unresolved','order_authority':False}
    _typed_member_inventory(receipt,namespaces,check)
    reference_text=structural_html_source_text(body,receipt,check=check)
    receipt.update(source_text_sha256=_sha(reference_text.encode('utf-8')),source_text_chars=len(reference_text),
        source_text_utf8_bytes=len(reference_text.encode('utf-8')),source_text_use='incorporation_locator_only_not_analysis')
    receipt['inventory_bytes']=0
    for _ in range(4):
        size=len(_json(receipt))
        if size==receipt['inventory_bytes']:break
        receipt['inventory_bytes']=size
    if size>max_inventory_bytes:_fail('inventory_limit')
    return receipt


def iter_html_structural_tokens(receipt):
    """Expand one compact source-bound token at a time; validate receipt first."""
    for row in receipt['tokens']:
        kind,start,end,digest_id=row[:4]
        token={'kind':kind,'start':start,'end':end,'sha256':receipt['token_hashes'][digest_id]}
        if kind in {'start','empty'}:
            attrs=receipt['attribute_sets'][row[5]]
            token.update(tag=row[4],attributes=attrs['attributes'],css=attrs['css'],implicit_closes=['p'] if row[6] else [])
        elif kind=='end':token.update(tag=row[4],implicit_closes=['p'] if row[5] else [])
        elif kind=='text':token['whitespace_only']=row[4]
        yield token


def structural_html_source_text(body, receipt, *, check=None):
    """All structural source text for incorporation locators, without visibility.

    Inline boundaries never introduce characters: split words remain intact.
    Explicit block boundaries separate source words. This is source continuity,
    not DOM/CSS layout, native extraction quality or analysis authority.
    """
    _pulse(check)
    if not isinstance(body,bytes) or len(body)!=receipt['input_bytes'] or _sha(body)!=receipt['input_sha256']:
        _fail('source_projection_body_mismatch')
    if not receipt['visual_presence']:
        from .filing_evidence import _visible_text
        return _visible_text(body,check=check)
    blocks={'html','head','title','body','div','p','br','hr','pre','blockquote','center',
        'h1','h2','h3','h4','h5','h6','ul','ol','li','dl','dt','dd'} | set(_TABLE)
    parts=[];pos=0
    for token in iter_html_structural_tokens(receipt):
        _pulse(check)
        start,end=token['start'],token['end']
        if start!=pos or not start<end<=len(body) or _sha(body[start:end])!=token['sha256']:
            _fail('source_projection_token_mismatch')
        if token['kind']=='text':parts.append(_entities(body[start:end].decode('utf-8')))
        elif token['kind'] in {'start','end','empty'} and token['tag'] in blocks:parts.append('\n')
        pos=end
    if pos!=len(body):_fail('source_projection_incomplete')
    return ' '.join(''.join(parts).split())


def validate_html_structural_receipt(receipt, *, check=None):
    """IPC schema/coverage only. Original re-extraction establishes authenticity."""
    keys={'version','input_sha256','input_bytes','tokens','resource_edges','resources','resource_decoded_bytes','token_hashes','attribute_sets',
        'obligations','visual_presence','native_text_complete','css_layout','css_property_semantics','font_availability','encoded_image_validity',
        'text_visibility','visual_meaning','order_authority','inventory_bytes',
        'source_text_sha256','source_text_chars','source_text_utf8_bytes','source_text_use'}
    def require(condition):
        if not condition:_fail('invalid_receipt')
    def digest(value):return isinstance(value,str) and re.fullmatch('[0-9a-f]{64}',value) is not None
    require(type(receipt) is dict and set(receipt)==keys and receipt['version']==VERSION)
    require(digest(receipt['input_sha256']) and type(receipt['input_bytes']) is int and 0<receipt['input_bytes']<=MAX_HTML_BYTES)
    require(receipt['native_text_complete'] is False and receipt['order_authority'] is False
        and receipt['visual_meaning']=='unresolved' and all(receipt[k]=='unverified' for k in ('css_layout','css_property_semantics','font_availability','encoded_image_validity','text_visibility')))
    require(type(receipt['tokens']) is list and bool(receipt['tokens']))
    require(type(receipt['token_hashes']) is list and all(digest(v) for v in receipt['token_hashes'])
        and len(set(receipt['token_hashes']))==len(receipt['token_hashes']))
    require(type(receipt['attribute_sets']) is list)
    for attrs in receipt['attribute_sets']:
        _pulse(check)
        require(type(attrs) is dict and set(attrs)=={'attributes','css'}
            and type(attrs['attributes']) is dict and type(attrs['css']) is list)
    require(len({_json(v) for v in receipt['attribute_sets']})==len(receipt['attribute_sets']))
    used_hashes=set();used_attributes=set()
    for row in receipt['tokens']:
        _pulse(check)
        require(type(row) is list and len(row)>=4 and row[0] in {'start','empty','end','text','comment','doctype','xml_declaration'})
        require(len(row)=={'start':7,'empty':7,'end':6,'text':5,'comment':4,'doctype':4,'xml_declaration':4}[row[0]])
        require(type(row[3]) is int and 0<=row[3]<len(receipt['token_hashes']))
        used_hashes.add(row[3])
        if row[0] in {'start','empty'}:
            require(type(row[5]) is int and 0<=row[5]<len(receipt['attribute_sets']) and type(row[6]) is bool)
            used_attributes.add(row[5])
        elif row[0]=='end':require(type(row[5]) is bool)
    require(used_hashes==set(range(len(receipt['token_hashes']))) and used_attributes==set(range(len(receipt['attribute_sets']))))
    pos=0;edges=[];obligations=set();stack=[];empty_p=False;outside_content=False;document_state={'html':False,'head':False,'body':False,'xml':False};namespaces={'xml':_XML}
    for index,token in enumerate(iter_html_structural_tokens(receipt)):
        _pulse(check)
        require(type(token) is dict and token.get('kind') in {'text','comment','doctype','xml_declaration','start','empty','end'})
        kind=token['kind'];expected={'kind','start','end','sha256'} | ({'tag','attributes','css','implicit_closes'} if kind in {'start','empty'} else {'tag','implicit_closes'} if kind=='end' else {'whitespace_only'} if kind=='text' else set())
        require(set(token)==expected and type(token['start']) is int and type(token['end']) is int
            and token['start']==pos and pos<token['end']<=receipt['input_bytes'] and digest(token['sha256']))
        pos=token['end']
        if kind in {'start','empty','end'}:
            require(type(token['tag']) is str and token['implicit_closes'] in ([],['p']))
            if token['implicit_closes']:
                require(not document_state['xml'] and bool(stack) and stack[-1]=='p' and empty_p)
                require(kind=='end' and len(stack)>1 and token['tag']==stack[-2]
                    or kind=='start' and token['tag'] in {'p','div','hr','table','ul','ol','dl','blockquote','center','h1','h2','h3','h4','h5','h6'})
                stack.pop();empty_p=False
        elif kind=='xml_declaration':
            require(stack==['xbrl'] and not document_state['html'] and not document_state['xml'])
            document_state['xml']=True
        elif kind=='text':
            require(type(token['whitespace_only']) is bool)
            if not stack and not token['whitespace_only']:
                require(not document_state.get('xbrl'));outside_content=True
            if stack and stack[-1]=='p' and not token['whitespace_only']:empty_p=False
        if kind in {'start','empty'}:
            tag=token['tag'];attrs=token['attributes'];require(type(attrs) is dict and all(type(k) is str and type(v) is str for k,v in attrs.items()))
            role=_attributes(tag,attrs,namespaces)
            if tag=='xbrl':require(not outside_content)
            require(token['css']==_css(attrs.get('style','')))
            if kind=='empty':require(document_state['xml'] and namespaces.get('')==_XHTML and tag not in _VOID)
            if tag=='html' and document_state['xml']:require(namespaces.get('')==_XHTML)
            _document_start(tag,stack,document_state)
            _nest(tag,stack,namespaces)
            require(len(stack)<64)
            if stack and stack[-1]=='p':empty_p=False
            if tag not in _VOID and kind!='empty':stack.append(tag)
            if tag=='p':empty_p=True
            obligations.update(_obligations(role,attrs,token['css']))
            if tag=='img':
                obligations.add('image_resource');edges.append({'token_index':index,'attribute':'src','filename':attrs.get('src','')})
        elif kind=='end':
            _role(token['tag'],namespaces)
            require(bool(stack) and stack.pop()==token['tag'])
    require(pos==receipt['input_bytes'] and not stack and receipt['resource_edges']==edges)
    _typed_member_inventory(receipt,namespaces,check)
    require(not document_state.get('xbrl') or document_state['html'])
    require(receipt['obligations']==sorted(obligations) and receipt['visual_presence'] is bool(obligations))
    require(type(receipt['resources']) is list and [r.get('filename') for r in receipt['resources']]==sorted({e['filename'] for e in edges}))
    for resource in receipt['resources']:
        _pulse(check)
        require(set(resource)=={'type','sequence','filename','body_start','body_end','body_sha256','transport','decoded_bytes','decoded_sha256','trimmed_zero_sextets','ignored_padding_sextets','declared_codec','encoded_image_validity'})
        require(resource['type'].upper()=='GRAPHIC' and resource['transport']=='sec_graphic_uu_v1'
            and resource['encoded_image_validity']=='unverified' and digest(resource['body_sha256']) and digest(resource['decoded_sha256']))
        require(all(type(resource[k]) is int and resource[k]>=0 for k in ('body_start','body_end','decoded_bytes','trimmed_zero_sextets','ignored_padding_sextets')))
        require(0<resource['body_end']-resource['body_start']<=MAX_RESOURCE_BYTES and 0<resource['decoded_bytes']<=MAX_RESOURCE_BYTES)
    require(receipt['source_text_use']=='incorporation_locator_only_not_analysis' and digest(receipt['source_text_sha256'])
        and all(type(receipt[k]) is int and 0<=receipt[k]<=MAX_INVENTORY_BYTES for k in ('source_text_chars','source_text_utf8_bytes')))
    require(receipt['resource_decoded_bytes']==sum(r['decoded_bytes'] for r in receipt['resources']))
    require(type(receipt['inventory_bytes']) is int and receipt['inventory_bytes']==len(_json(receipt)) and receipt['inventory_bytes']<=MAX_INVENTORY_BYTES)
