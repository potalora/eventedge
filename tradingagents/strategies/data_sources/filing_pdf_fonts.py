"""Finite native PDF font declarations, not glyph-shape or hint execution.

All decoded streams are supplied by the caller's aggregate-accounting callback.
The SFNT reader supports quadratic TrueType outlines only. No external fonts,
network, native font engine, hint interpreter, or permissive cmap fallback.
"""
from __future__ import annotations
import hashlib
import json
import math
import re
import struct

VERSION = 'sec-pdf-declared-font-v2'
_STANDARD = {f'/{x}' for x in ('Helvetica', 'Helvetica-Bold', 'Helvetica-Oblique', 'Helvetica-BoldOblique',
    'Times-Roman', 'Times-Bold', 'Times-Italic', 'Times-BoldItalic', 'Courier', 'Courier-Bold', 'Courier-Oblique', 'Courier-BoldOblique')}

class FontError(ValueError):
    pass

def fail(reason='pdf_invalid_embedded_font'):
    raise FontError(reason)

def obj(value):
    return value.get_object() if hasattr(value, 'get_object') else value

def integer(value, low=0, high=65535):
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high

def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)

def sha(value):
    return hashlib.sha256(value).hexdigest()

def unicode_text(value):
    if not value or any(ord(c) < 32 or 127 <= ord(c) <= 159 or 0xD800 <= ord(c) <= 0xDFFF
                        or 0xE000 <= ord(c) <= 0xF8FF or ord(c) in (0xFFFD, 0xFFFE, 0xFFFF) for c in value):
        fail('pdf_font_unicode_mapping_invalid')
    return value

class TrueType:
    def __init__(self, data, check):
        self.data, self.check, self.tables, self.glyphs = data, check, {}, {}
        if len(data) < 12 or data[:4] != b'\x00\x01\x00\x00': fail('pdf_unsupported_sfnt')
        n, search, selector, shift = struct.unpack_from('>HHHH', data, 4)
        if not 1 <= n <= 64 or 12+n*16 > len(data): fail()
        power = 1 << (n.bit_length()-1)
        self.directory_hints = {'declared':[search,selector,shift],
            'derived':[power*16,power.bit_length()-1,n*16-power*16],
            'used':'derived_from_num_tables'}
        occupied = []
        previous_tag = None
        self.inventory = []
        for i in range(n):
            check()
            tag, checksum, offset, length = struct.unpack_from('>4sIII', data, 12+16*i)
            if previous_tag is not None and tag <= previous_tag: fail('pdf_font_directory_order')
            previous_tag = tag
            if tag in self.tables or offset % 4 or offset < 12+n*16 or offset+length > len(data): fail()
            if any(offset < end and start < offset+length for start,end in occupied): fail()
            occupied.append((offset,offset+length))
            table = data[offset:offset+length]
            verify = table[:8] + b'\0'*4 + table[12:] if tag == b'head' and len(table)>=12 else table
            padded = verify + b'\0'*((-len(verify))%4)
            actual = sum(v[0] for v in struct.iter_unpack('>I',padded)) & 0xffffffff
            if actual != checksum: fail('pdf_font_table_checksum')
            self.tables[tag] = table
            self.inventory.append({'tag':tag.decode('ascii'), 'bytes':length,'sha256':sha(table)})
        if not self.tables.keys() <= {b'head',b'maxp',b'hhea',b'hmtx',b'loca',b'glyf',b'cmap',
                b'OS/2',b'cvt ',b'fpgm',b'prep',b'meta',b'name',b'post',b'gasp'}:
            fail('pdf_unsupported_font_table')
        if not {b'head',b'maxp',b'hhea',b'hmtx',b'loca',b'glyf',b'cmap'} <= self.tables.keys(): fail()
        head,maxp,hhea = (self.tables[x] for x in (b'head',b'maxp',b'hhea'))
        if len(head)!=54 or len(maxp)!=32 or len(hhea)!=36: fail()
        if head[:4]!=b'\0\1\0\0' or head[12:16]!=b'_\x0f<\xf5' or maxp[:4]!=b'\0\1\0\0' or hhea[:4]!=b'\0\1\0\0': fail()
        self.units = struct.unpack_from('>H',head,18)[0]
        self.count = struct.unpack_from('>H',maxp,4)[0]
        fmt = struct.unpack_from('>h',head,50)[0]
        metrics = struct.unpack_from('>H',hhea,34)[0]
        if not 16<=self.units<=16384 or not self.count or fmt not in (0,1) or not 1<=metrics<=self.count: fail()
        if struct.unpack_from('>h',head,52)[0] or struct.unpack_from('>h',hhea,32)[0]: fail()
        loca = self.tables[b'loca']
        if len(loca)!=(self.count+1)*(2 if fmt==0 else 4): fail()
        self.offsets = [v[0]*(2 if fmt==0 else 1) for v in struct.iter_unpack('>H' if fmt==0 else '>I',loca)]
        if self.offsets != sorted(self.offsets) or self.offsets[-1] > len(self.tables[b'glyf']): fail()
        hmtx=self.tables[b'hmtx']
        if len(hmtx)!=metrics*4+(self.count-metrics)*2: fail()
        self.advances=[struct.unpack_from('>H',hmtx,min(i,metrics-1)*4)[0] for i in range(self.count)]
        padded = data + b'\0'*((-len(data))%4)
        if sum(v[0] for v in struct.iter_unpack('>I',padded)) & 0xffffffff != 0xB1B0AFBA:
            fail('pdf_font_checksum_adjustment')
        self.cmaps=self._cmaps()

    def _cmaps(self):
        data=self.tables[b'cmap']
        if len(data)<4 or data[:2]!=b'\0\0': fail()
        count=struct.unpack_from('>H',data,2)[0]
        if not 1<=count<=32 or 4+8*count>len(data): fail()
        maps=[]
        for i in range(count):
            self.check()
            platform,encoding,offset=struct.unpack_from('>HHI',data,4+8*i)
            if offset<4+8*count or offset+6>len(data): fail()
            fmt,length,language=struct.unpack_from('>HHH',data,offset)
            if length<6 or offset+length>len(data): fail()
            table=data[offset:offset+length]
            if (platform,encoding)==(1,0) and fmt==0:
                if length!=262 or any(g>=self.count for g in table[6:]): fail()
                continue
            if (platform,encoding) not in ((3,1),(0,3)) or fmt!=4: fail('pdf_unsupported_font_cmap')
            if len(table)<16: fail()
            seg2,srch,sel,shift=struct.unpack_from('>HHHH',table,6)
            if not seg2 or seg2%2: fail()
            n=seg2//2; power=1<<(n.bit_length()-1)
            if (srch,sel,shift)!=(power*2,power.bit_length()-1,seg2-power*2) or 16+8*n>len(table): fail()
            ends=struct.unpack_from('>'+'H'*n,table,14)
            starts=struct.unpack_from('>'+'H'*n,table,16+2*n)
            deltas=struct.unpack_from('>'+'h'*n,table,16+4*n)
            ranges=struct.unpack_from('>'+'H'*n,table,16+6*n)
            if table[14+2*n:16+2*n]!=b'\0\0' or ends[-1]!=65535: fail()
            mapped={}; previous=-1
            for j,(start,end,delta,ran) in enumerate(zip(starts,ends,deltas,ranges)):
                self.check()
                if start>end or start<=previous or ran%2: fail()
                previous=end
                for code in range(start,end+1):
                    if ran:
                        pos=16+6*n+2*j+ran+2*(code-start)
                        if pos<16+8*n or pos+2>len(table): fail()
                        gid=struct.unpack_from('>H',table,pos)[0]
                        gid=(gid+delta)&65535 if gid else 0
                    else: gid=(code+delta)&65535
                    if gid>=self.count: fail('pdf_font_missing_glyph')
                    if gid: mapped[code]=gid
            maps.append(mapped)
        if not maps: fail('pdf_font_unicode_cmap_missing')
        if any(m!=maps[0] for m in maps[1:]): fail('pdf_font_ambiguous_cmap')
        return maps[0]

    def glyph(self,gid,active=()):
        self.check()
        if not integer(gid,1,self.count-1): fail('pdf_font_missing_glyph')
        if gid in self.glyphs:return self.glyphs[gid]
        if gid in active or len(active)>=32: fail('pdf_font_cyclic_glyph')
        data=self.tables[b'glyf'][self.offsets[gid]:self.offsets[gid+1]]
        if not data:
            result={'declared_bbox':[0,0,0,0], 'outline_bytes':0}
            self.glyphs[gid]=result;return result
        if len(data)<10: fail()
        contours,*bbox=struct.unpack_from('>hhhhh',data)
        if bbox[0]>bbox[2] or bbox[1]>bbox[3]: fail()
        pos=10
        if contours>=0:
            if pos+2*contours+2>len(data): fail()
            ends=list(struct.unpack_from('>'+'H'*contours,data,pos))
            if any(a>=b for a,b in zip(ends,ends[1:])): fail()
            points=ends[-1]+1 if ends else 0
            pos+=2*contours
            instructions=struct.unpack_from('>H',data,pos)[0];pos+=2+instructions
            if pos>len(data): fail()
            flags=[]
            while len(flags)<points:
                if pos>=len(data): fail()
                flag=data[pos];pos+=1
                if flag&0xC0: fail()
                repeat=1
                if flag&8:
                    if pos>=len(data): fail()
                    repeat+=data[pos];pos+=1
                if len(flags)+repeat>points: fail()
                flags.extend([flag]*repeat)
            xs=[];ys=[]
            for short,same,coords in ((2,16,xs),(4,32,ys)):
                value=0
                for flag in flags:
                    if flag&short:
                        if pos>=len(data): fail()
                        delta=data[pos]*(1 if flag&same else -1);pos+=1
                    elif flag&same: delta=0
                    else:
                        if pos+2>len(data): fail()
                        delta=struct.unpack_from('>h',data,pos)[0];pos+=2
                    value+=delta;coords.append(value)
            if xs and (min(xs)<bbox[0] or max(xs)>bbox[2] or min(ys)<bbox[1] or max(ys)>bbox[3]): fail('pdf_font_glyph_bounds')
        elif contours==-1:
            flags=32
            while flags&32:
                if pos+4>len(data): fail()
                flags,child=struct.unpack_from('>HH',data,pos);pos+=4
                if flags&~0x1FEF or sum(bool(flags&x) for x in (8,64,128))>1: fail()
                if not flags&2:fail('pdf_unsupported_composite_point_attachment')
                if flags&2048 and flags&4096:fail('pdf_invalid_composite_offset_flags')
                pos+=4 if flags&1 else 2
                pos+=2 if flags&8 else 4 if flags&64 else 8 if flags&128 else 0
                if pos>len(data): fail()
                self.glyph(child,(*active,gid))
            if flags&256:
                if pos+2>len(data): fail()
                pos+=2+struct.unpack_from('>H',data,pos)[0]
        else: fail()
        # SFNT producers can align individual glyphs with up to 3 null bytes.
        if pos>len(data) or len(data)-pos>3 or any(data[pos:]): fail('pdf_font_glyph_structure')
        result={'declared_bbox':bbox,'outline_bytes':len(data)}
        self.glyphs[gid]=result
        return result


def cmap_mapping(data, code_bytes, check=lambda:None):
    """Finite non-executable ToUnicode CMap grammar with exact consumption."""
    try: text=data.decode('ascii')
    except UnicodeDecodeError: fail('pdf_invalid_tounicode')
    text=re.sub(r'%[^\r\n]*','',text)
    pre=r'\s*/CIDInit\s+/ProcSet\s+findresource\s+begin\s+\d+\s+dict\s+begin\s+begincmap\s*'
    match=re.match(pre,text)
    if not match: fail('pdf_unsupported_tounicode_grammar')
    text=text[match.end():]
    declarations=[r'/CIDSystemInfo\s*<<\s*/Registry\s*\([^()]*\)\s*/Ordering\s*\([^()]*\)\s*/Supplement\s+\d+\s*>>\s*def',
                  r'/CMapName\s+/[A-Za-z0-9_.-]+\s+def',r'/CMapType\s+2\s+def']
    for pattern in declarations:
        m=re.match(r'\s*'+pattern,text)
        if not m: fail('pdf_unsupported_tounicode_grammar')
        text=text[m.end():]
    mapping={};spaces=[]
    def hexbytes(token):
        if not re.fullmatch(r'<(?:[0-9A-Fa-f]{2})+>',token): fail('pdf_invalid_tounicode')
        return bytes.fromhex(token[1:-1])
    while True:
        m=re.match(r'\s*(\d+)\s+begin(codespacerange|bfchar|bfrange)\b',text)
        if not m: break
        count=int(m[1]);kind=m[2]
        if not 1<=count<=65536: fail('pdf_invalid_tounicode')
        end=re.search(r'\bend'+kind+r'\b',text[m.end():])
        if not end: fail('pdf_invalid_tounicode')
        body=text[m.end():m.end()+end.start()]
        tokens=re.findall(r'<[^<>]*>|\[|\]|\S+',body)
        index=0
        def take():
            nonlocal index
            if index>=len(tokens): fail('pdf_invalid_tounicode')
            token=tokens[index];index+=1;return token
        def code():
            raw=hexbytes(take())
            if len(raw)!=code_bytes:fail('pdf_invalid_tounicode')
            return int.from_bytes(raw,'big')
        def put(c,raw):
            check()
            if len(raw)>16:fail('pdf_unsupported_tounicode_sequence')
            if c in mapping:fail('pdf_font_ambiguous_tounicode')
            try:value=raw.decode('utf-16-be')
            except UnicodeDecodeError:fail('pdf_invalid_tounicode')
            mapping[c]=unicode_text(value)
        for _ in range(count):
            start=code()
            if kind=='bfchar':put(start,hexbytes(take()));continue
            last=code()
            if last<start: fail('pdf_invalid_tounicode')
            if kind=='codespacerange':spaces.append((start,last));continue
            target=take()
            if target=='[':
                for c in range(start,last+1):put(c,hexbytes(take()))
                if take()!=']':fail('pdf_invalid_tounicode')
            else:
                raw=hexbytes(target);base=int.from_bytes(raw,'big')
                for c in range(start,last+1):
                    try:put(c,(base+c-start).to_bytes(len(raw),'big'))
                    except OverflowError:fail('pdf_invalid_tounicode')
        if index!=len(tokens):fail('pdf_invalid_tounicode')
        text=text[m.end()+end.end():]
    if not re.fullmatch(r'\s*endcmap\s+CMapName\s+currentdict\s+/CMap\s+defineresource\s+pop\s+end\s+end\s*',text):fail('pdf_unsupported_tounicode_grammar')
    if not spaces or any(a<=d and c<=b for i,(a,b) in enumerate(spaces) for c,d in spaces[:i]):fail('pdf_invalid_tounicode')
    if any(not any(a<=code<=b for a,b in spaces) for code in mapping):fail('pdf_invalid_tounicode')
    return mapping


class FontDecoder:
    def __init__(self, font, consume_stream, check):
        self.check=check;self.widths={};self.ttf=None;self.cidmap=None;self.mapping=None;self.code_bytes=1
        self.standard=False;self.receipt={'version':VERSION,'mapping_trust':'source_declared_unicode','hint_programs_executed':False}
        from pypdf.generic import NameObject, TextStringObject
        subtype=font.get('/Subtype');encoding=obj(font.get('/Encoding'))
        for key in ('/Type','/Subtype','/Name','/BaseFont','/Encoding'):
            if key in font and not isinstance(obj(font[key]),NameObject):fail('pdf_invalid_font_declaration')
        allowed={'/Type','/Subtype','/Name','/BaseFont','/Encoding','/FirstChar','/LastChar','/Widths',
                 '/FontDescriptor','/ToUnicode','/DescendantFonts'}
        if not isinstance(font,dict) or not set(font)<=allowed:fail('pdf_unsupported_font_dependency')
        self.receipt.update(subtype=str(subtype),base_font=str(font.get('/BaseFont','')))
        if subtype=='/Type1' and font.get('/BaseFont') in _STANDARD and '/FontDescriptor' not in font and encoding in (None,'/StandardEncoding','/WinAnsiEncoding') and '/ToUnicode' not in font:
            from pypdf._codecs import _win_encoding,_std_encoding
            from pypdf._codecs.core_font_metrics import CORE_FONT_METRICS
            self.core_widths=CORE_FONT_METRICS[str(font['/BaseFont'])[1:]].character_widths
            self.encoding=_win_encoding if encoding=='/WinAnsiEncoding' else _std_encoding
            self.standard=True
            self.receipt['kind']='standard14_declared'
            self.receipt['encoding']=str(encoding or '/StandardEncoding')
            self.receipt['core_metrics_sha256']=sha(json.dumps(self.core_widths,sort_keys=True).encode())
            if any(k in font for k in ('/Widths','/FirstChar','/LastChar')):self._simple_widths(font)
            self.receipt['declared_widths_sha256']=sha(json.dumps(self.widths,sort_keys=True).encode())
            return
        if subtype=='/TrueType' and encoding=='/WinAnsiEncoding':
            from pypdf._codecs import _win_encoding
            self.encoding=_win_encoding;self._simple_widths(font)
            descriptor=obj(font.get('/FontDescriptor'))
            if '/ToUnicode' in font:self.mapping=cmap_mapping(consume_stream(obj(font['/ToUnicode']),'font_cmap'),1,check)
        elif subtype=='/Type0' and encoding=='/Identity-H':
            self.code_bytes=2
            descendants=obj(font.get('/DescendantFonts'))
            if not isinstance(descendants,list) or len(descendants)!=1:fail('pdf_invalid_cid_font')
            descendant=obj(descendants[0])
            if not isinstance(descendant,dict) or not set(descendant)<={'/Type','/Subtype','/BaseFont','/CIDSystemInfo','/FontDescriptor','/DW','/W','/CIDToGIDMap'}:fail('pdf_unsupported_cid_dependency')
            if descendant.get('/Subtype')!='/CIDFontType2':fail('pdf_unsupported_cid_font')
            system=obj(descendant.get('/CIDSystemInfo'))
            if not isinstance(system,dict) or set(system)!={'/Registry','/Ordering','/Supplement'} or not isinstance(system['/Registry'],TextStringObject) or not isinstance(system['/Ordering'],TextStringObject) or not integer(system['/Supplement']):fail('pdf_invalid_cid_system')
            if '/ToUnicode' not in font:fail('pdf_unverified_font_encoding')
            self.mapping=cmap_mapping(consume_stream(obj(font['/ToUnicode']),'font_cmap'),2,check)
            descriptor=obj(descendant.get('/FontDescriptor'))
            self.default_width=descendant.get('/DW',1000)
            if not number(self.default_width) or self.default_width<0:fail('pdf_invalid_font_width')
            widths=obj(descendant.get('/W',[]));index=0
            if not isinstance(widths,list):fail('pdf_invalid_font_width')
            while index<len(widths):
                check();start=widths[index];index+=1
                if not integer(start) or index>=len(widths):fail('pdf_invalid_font_width')
                value=obj(widths[index]);index+=1
                if isinstance(value,list):items=list(enumerate(value,start))
                else:
                    if not integer(value,start) or index>=len(widths):fail('pdf_invalid_font_width')
                    items=((c,widths[index]) for c in range(start,value+1));index+=1
                for c,width in items:
                    if c in self.widths or not integer(c) or not number(width) or width<0:fail('pdf_invalid_font_width')
                    self.widths[c]=float(width)
            cidmap=obj(descendant.get('/CIDToGIDMap','/Identity'))
            if cidmap!='/Identity':
                self.cidmap=consume_stream(cidmap,'font_cid_map')
                if len(self.cidmap)%2:fail('pdf_invalid_cid_map')
        else:fail('pdf_unverified_font_encoding')
        if not isinstance(descriptor,dict) or '/FontFile2' not in descriptor or any(k in descriptor for k in ('/FontFile','/FontFile3')):fail('pdf_unverified_embedded_font')
        if not set(descriptor)<={'/Type','/FontName','/FontFamily','/FontStretch','/FontWeight','/Flags','/FontBBox',
                '/ItalicAngle','/Ascent','/Descent','/Leading','/CapHeight','/XHeight','/StemV','/StemH','/AvgWidth',
                '/MaxWidth','/MissingWidth','/FontFile2'}:fail('pdf_unsupported_font_descriptor')
        for key,value in descriptor.items():
            if key not in ('/Type','/FontName','/FontFamily','/FontStretch','/FontBBox','/FontFile2') and not number(value):fail('pdf_invalid_font_descriptor')
        if descriptor.get('/Type','/FontDescriptor')!='/FontDescriptor' or not isinstance(descriptor.get('/FontName'),NameObject):fail('pdf_invalid_font_descriptor')
        flags=descriptor.get('/Flags')
        if not integer(flags,0,2**31-1) or not flags&32 or flags&4:fail('pdf_unsupported_symbolic_font')
        bbox=obj(descriptor.get('/FontBBox'))
        if not isinstance(bbox,list) or len(bbox)!=4 or not all(number(v) for v in bbox) or bbox[0]>bbox[2] or bbox[1]>bbox[3]:fail('pdf_invalid_font_descriptor')
        data=consume_stream(obj(descriptor['/FontFile2']),'embedded_font')
        self.ttf=TrueType(data,check)
        self.receipt.update(kind='embedded_truetype_declared',font_sha256=sha(data),font_bytes=len(data),tables=self.ttf.inventory,
                            declared_font_bbox=list(bbox),units_per_em=self.ttf.units,code_bytes=self.code_bytes,
                            directory_hints=self.ttf.directory_hints,encoding=str(encoding),
                            declared_mapping_sha256=sha(json.dumps(self.mapping,sort_keys=True).encode()),
                            declared_widths_sha256=sha(json.dumps(self.widths,sort_keys=True).encode()),
                            cid_map_sha256=sha(self.cidmap) if self.cidmap is not None else None,
                            default_width=getattr(self,'default_width',None))

    def _simple_widths(self,font):
        first,last=font.get('/FirstChar'),font.get('/LastChar');widths=obj(font.get('/Widths'))
        if not integer(first,0,255) or not integer(last,first,255) or not isinstance(widths,list) or len(widths)!=last-first+1:fail('pdf_invalid_font_width')
        for c,width in enumerate(widths,first):
            if not number(width) or width<0:fail('pdf_invalid_font_width')
            self.widths[c]=float(width)

    def decode(self,raw, provenance_budget=None):
        if provenance_budget is not None and len(raw)//self.code_bytes*128>provenance_budget:
            fail('pdf_stream_limit_provenance')
        if len(raw)%self.code_bytes:fail('pdf_font_partial_character')
        glyphs=[];text=[]
        for offset in range(0,len(raw),self.code_bytes):
            self.check();code=int.from_bytes(raw[offset:offset+self.code_bytes],'big')
            value=self.mapping.get(code) if self.mapping is not None else self.encoding[code]
            if value is None:fail('pdf_font_unmapped_character')
            value=unicode_text(value)
            if self.standard:
                if value not in self.core_widths:fail('pdf_font_missing_core_metric')
                glyph={'code':code,'unicode':value,'glyph_id':None,'width':self.widths.get(code,self.core_widths[value]),'declared_bbox':None}
            else:
                if self.code_bytes==1:
                    # ToUnicode cannot contradict this finite WinAnsi path.
                    if value!=unicode_text(self.encoding[code]):fail('pdf_font_contradictory_mapping')
                    gid=self.ttf.cmaps.get(ord(value),0)
                    if code not in self.widths:fail('pdf_font_missing_width')
                else:
                    if self.cidmap is None:gid=code
                    elif code*2+2<=len(self.cidmap):gid=int.from_bytes(self.cidmap[code*2:code*2+2],'big')
                    else:fail('pdf_font_missing_glyph')
                    if len(value)==1 and self.ttf.cmaps.get(ord(value))!=gid:fail('pdf_font_contradictory_mapping')
                outline=self.ttf.glyph(gid)
                width=self.widths.get(code,getattr(self,'default_width',None))
                actual=self.ttf.advances[gid]*1000/self.ttf.units
                if width is None or abs(width-actual)>1.01:fail('pdf_font_width_mismatch')
                glyph={'code':code,'unicode':value,'glyph_id':gid,'width':width,**outline}
            glyphs.append(glyph);text.append(value)
        return {'text':''.join(text),'glyphs':glyphs}


def build_font(font, *, consume_stream, check):
    try:return FontDecoder(font,consume_stream,check)
    except FontError:raise
    except (KeyError,IndexError,TypeError,UnicodeError,struct.error,OverflowError):
        fail()
