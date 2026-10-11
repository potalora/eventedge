"""Prospective structural visual presence; never image or native-text validity.

Call only inside the existing bounded parser child. JPEG entropy, raster pixels,
font programs, Unicode and visibility are deliberately uninterpreted. The receipt
proves complete acquisition/PDF/content/encoded-image framing within this finite
subset and records positive image invocations. It cannot support text analysis
or orders, and must be recomputed from the retained original during audit.
"""
from __future__ import annotations
import io
import json
import math
import zlib
from . import filing_pdf as native

VERSION = 'sec-pdf-structural-visual-v1'

class FilingPDFVisualError(ValueError):
    pass

def fail(code):
    raise FilingPDFVisualError('pdf_visual_' + code)

def integer(value, low=0):
    return isinstance(value,int) and not isinstance(value,bool) and value>=low

def _reserve(state, size):
    state['inventory_bytes'] += size
    if state['inventory_bytes'] > state['limit']:fail('inventory_limit')

def _record(value, state, active=(), depth=0):
    """Typed source declarations, never inferred Unicode or font rendering."""
    from pypdf.generic import NameObject, BooleanObject, NullObject, TextStringObject, ByteStringObject
    native._pulse(state['check'])
    value=native._object(value)
    # Charge while constructing, before repeated shared-resource declarations
    # can expand a small source graph into an unbounded Python/JSON tree.
    _reserve(state, 2)
    if isinstance(value,str):_reserve(state, len(value.encode('utf-8',errors='surrogatepass'))*6)
    elif isinstance(value,bytes):_reserve(state,len(value)*2+128)
    if depth>32 or id(value) in active:fail('resource_graph_cycle_or_depth')
    if isinstance(value,NameObject):return str(value)
    if isinstance(value,BooleanObject):return value.value
    if value is None or isinstance(value,NullObject):return None
    if isinstance(value,(TextStringObject,ByteStringObject)):
        raw=native._raw_text(value)
        if len(raw)*2>state['limit']-state['inventory_bytes']:fail('inventory_limit')
        return {'raw_hex':raw.hex(),'bytes':len(raw),'sha256':native._sha(raw)}
    if isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value):return int(value) if isinstance(value,int) else float(value)
    if isinstance(value,list):return [_record(v,state,(*active,id(value)),depth+1) for v in value]
    if isinstance(value,dict):
        if hasattr(value,'_data') and not set(value)<={'/Length','/Filter','/DecodeParms','/Length1','/Length2','/Length3','/Subtype'}:fail('unsupported_resource_stream_dependency')
        for key in value:_reserve(state,len(str(key))*6+3)
        result={str(k):_record(v,state,(*active,id(value)),depth+1) for k,v in value.items()}
        if hasattr(value,'_data'):
            data=_decode_stream(value,state,'resource')
            result['stream']={'encoded_sha256':native._sha(value._data),'encoded_bytes':len(value._data),
                              'decoded_sha256':native._sha(data),'decoded_bytes':len(data)}
        return result
    fail('unrecorded_resource_type')

def _append(state,target,value):
    size=len(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode())
    state['inventory_bytes']+=size
    if state['inventory_bytes']>state['limit']:fail('inventory_limit')
    target.append(value)

def _filters(stream):
    from pypdf.generic import NameObject,NullObject
    filters=native._object(stream.get('/Filter',[]))
    if isinstance(filters,NameObject):filters=[filters]
    if not isinstance(filters,list) or any(not isinstance(f,NameObject) for f in filters):fail('invalid_filter')
    params=native._object(stream.get('/DecodeParms'))
    if params is not None and not isinstance(params,NullObject):
        # No predictors, transforms or per-filter recovery in this finite subset.
        if not isinstance(params,list) or len(params)!=len(filters) or any(not isinstance(native._object(p),NullObject) for p in params):
            fail('unsupported_filter_parameters')
    return list(map(str,filters))

def _decode_stream(stream,state,kind,*,image=False):
    native._pulse(state['check'])
    if not isinstance(stream,dict) or not hasattr(stream,'_data') or not isinstance(stream._data,bytes):fail('invalid_stream')
    filters=_filters(stream)
    lossless=filters[:-1] if image and filters and filters[-1] in ('/DCTDecode','/DCT') else filters
    if lossless not in ([],['/FlateDecode'],['/Fl']):fail('unsupported_filter_chain')
    data=stream._data
    if len(data)>state['limit']:fail('stream_limit')
    if lossless:
        decoder=zlib.decompressobj()
        remaining=state['limit']-state['decoded_stream_bytes']
        try:data=decoder.decompress(data,remaining+1)
        except zlib.error:fail('invalid_flate')
        if len(data)>remaining or decoder.unconsumed_tail:fail('stream_limit')
        if not decoder.eof or decoder.unused_data:fail('incomplete_or_trailing_flate')
    state['decoded_stream_bytes']+=len(data)
    if state['decoded_stream_bytes']>state['limit']:fail('stream_limit')
    native._pulse(state['check'])
    return data

def _jpeg(data,width,height,components,bits,state):
    """Complete marker/segment framing only. Entropy bytes are opaque by design."""
    if not data.startswith(b'\xff\xd8'):fail('jpeg_soi')
    pos=2;frame=None;scans=[];segments=[];tables=set();quant=set();ended=False
    while pos<len(data):
        native._pulse(state['check'])
        start=pos
        if data[pos]!=255:fail('jpeg_marker_boundary')
        while pos<len(data) and data[pos]==255:pos+=1
        if pos>=len(data):fail('jpeg_truncated_marker')
        marker=data[pos];pos+=1
        if marker==0xd9:
            if pos!=len(data) or frame is None or not scans:fail('jpeg_early_or_trailing_eoi')
            ended=True;break
        if marker not in (0xc0,0xc2,0xc4,0xdb,0xda,0xdd,0xe0,0xee,0xfe):fail('jpeg_unsupported_marker')
        if pos+2>len(data):fail('jpeg_truncated_segment')
        length=int.from_bytes(data[pos:pos+2],'big')
        if length<2 or pos+length>len(data):fail('jpeg_segment_length')
        segment=data[pos+2:pos+length];pos+=length
        _append(state,segments,{'marker':marker,'start':start,'end':pos,'sha256':native._sha(data[start:pos])})
        if len(segments)>state['limit']//8:fail('inventory_limit')
        if marker in (0xc0,0xc2):
            if frame is not None or len(segment)<6:fail('jpeg_frame')
            precision=segment[0];h=int.from_bytes(segment[1:3],'big');w=int.from_bytes(segment[3:5],'big');n=segment[5]
            if (precision,w,h,n)!=(bits,width,height,components) or precision!=8 or n not in (1,3,4) or len(segment)!=6+3*n:fail('jpeg_pdf_frame_mismatch')
            declared=[]
            for offset in range(6,len(segment),3):
                ident,sampling,q=segment[offset:offset+3]
                if ident in [r['id'] for r in declared] or not 1<=sampling>>4<=4 or not 1<=sampling&15<=4 or q>3:fail('jpeg_component')
                declared.append({'id':ident,'horizontal_sampling':sampling>>4,'vertical_sampling':sampling&15,'quantization_table':q})
            frame={'marker':marker,'precision':precision,'width':w,'height':h,'components':declared}
        elif marker==0xdb:
            offset=0
            while offset<len(segment):
                table=segment[offset];offset+=1;p=table>>4;q=table&15
                if p not in (0,1) or q>3 or offset+64*(p+1)>len(segment):fail('jpeg_quantization_framing')
                values=segment[offset:offset+64*(p+1)]
                if any(int.from_bytes(values[i:i+p+1],'big')==0 for i in range(0,len(values),p+1)):fail('jpeg_quantization_value')
                quant.add(q);offset+=64*(p+1)
        elif marker==0xc4:
            offset=0
            while offset<len(segment):
                if offset+17>len(segment):fail('jpeg_huffman_framing')
                table=segment[offset];counts=segment[offset+1:offset+17];offset+=17;count=sum(counts)
                if table>>4 not in (0,1) or table&15>3 or not 1<=count<=256 or offset+count>len(segment):fail('jpeg_huffman_framing')
                slots=1
                for n in counts:
                    slots=slots*2-n
                    if slots<0:fail('jpeg_huffman_framing')
                tables.add((table>>4,table&15));offset+=count
        elif marker==0xdd and len(segment)!=2:fail('jpeg_restart_interval')
        elif marker==0xda:
            if frame is None or not segment or not 1<=segment[0]<=4 or len(segment)!=1+2*segment[0]+3:fail('jpeg_scan_framing')
            count=segment[0];selectors=segment[1:-3];ss,se,approx=segment[-3:];ah,al=approx>>4,approx&15
            ids=[selectors[i] for i in range(0,len(selectors),2)]
            if len(set(ids))!=count or not set(ids)<={c['id'] for c in frame['components']} or not 0<=ss<=se<=63 or ah>13 or al>13:fail('jpeg_scan_parameters')
            if frame['marker']==0xc0 and (ss,se,ah,al)!=(0,63,0,0):fail('jpeg_scan_parameters')
            if frame['marker']==0xc2 and ((ss==0 and se!=0) or (ss and count!=1) or (ah and ah!=al+1)):fail('jpeg_scan_parameters')
            if not {c['quantization_table'] for c in frame['components']}<=quant:fail('jpeg_missing_table')
            for offset in range(0,len(selectors),2):
                select=selectors[offset+1]
                if select>>4>3 or select&15>3:fail('jpeg_scan_parameters')
                needed=[]
                if ss==0 and ah==0:needed.append((0,select>>4))
                if se>0:needed.append((1,select&15))
                if any(t not in tables for t in needed):fail('jpeg_missing_table')
            entropy_start=pos;restart_count=0
            while pos<len(data):
                # Each scan remains under the inherited callback/deadline.
                if (pos-entropy_start)%4096==0:native._pulse(state['check'])
                if data[pos]!=255:pos+=1;continue
                if pos+1>=len(data):fail('jpeg_truncated_entropy_framing')
                if data[pos+1]==0:pos+=2;continue
                if 0xd0<=data[pos+1]<=0xd7:restart_count+=1;pos+=2;continue
                break
            if pos==entropy_start:fail('jpeg_empty_scan')
            _append(state,scans,{'components':ids,'spectral_start':ss,'spectral_end':se,'successive_high':ah,'successive_low':al,
                          'entropy_start':entropy_start,'entropy_end':pos,'entropy_sha256':native._sha(data[entropy_start:pos]),'restart_markers':restart_count})
    if not ended:fail('jpeg_missing_eoi')
    return {'frame':frame,'segments':segments,'scans':scans,'framing':'complete',
            'entropy_validation':'not_performed','pixel_validation':'not_performed','sha256':native._sha(data),'bytes':len(data)}

def _image(obj,resources,state):
    from pypdf.generic import BooleanObject,NameObject
    if not isinstance(obj.get('/Subtype'),NameObject) or obj['/Subtype']!='/Image':fail('invalid_image_subtype')
    if '/Type' in obj and (not isinstance(obj['/Type'],NameObject) or obj['/Type']!='/XObject'):fail('invalid_image_type')
    allowed={'/Type','/Subtype','/Width','/Height','/BitsPerComponent','/ColorSpace','/Filter','/DecodeParms','/Decode','/Length','/ImageMask','/Interpolate','/Intent'}
    if not set(obj)<=allowed:fail('unsupported_image_dependency')
    if any(not integer(obj.get(k),1) for k in ('/Width','/Height','/BitsPerComponent')):fail('image_dimensions')
    if obj['/BitsPerComponent'] not in (1,2,4,8,16):fail('image_bit_depth')
    if '/ImageMask' in obj and (not isinstance(obj['/ImageMask'],BooleanObject) or obj['/ImageMask'].value):fail('image_mask')
    if '/Interpolate' in obj and not isinstance(obj['/Interpolate'],BooleanObject):fail('image_interpolate')
    if '/Intent' in obj and obj['/Intent'] not in native._RENDER_INTENTS:fail('image_intent')
    components=native._color_components(obj.get('/ColorSpace'),resources)
    if '/Decode' in obj and not native._numbers(native._object(obj['/Decode']),components*2):fail('image_decode_array')
    filters=_filters(obj);is_jpeg=bool(filters and filters[-1] in ('/DCTDecode','/DCT'))
    data=_decode_stream(obj,state,'image',image=True)
    declared=((obj['/Width']*components*obj['/BitsPerComponent']+7)//8)*obj['/Height']
    jpeg=_jpeg(data,obj['/Width'],obj['/Height'],components,obj['/BitsPerComponent'],state) if is_jpeg else None
    if not is_jpeg and len(data)!=declared:fail('raw_image_length')
    return {'encoded_sha256':native._sha(obj._data),'encoded_bytes':len(obj._data),'decoded_sha256':native._sha(data),'decoded_bytes':len(data),
            'width':int(obj['/Width']),'height':int(obj['/Height']),'components':components,'bits':int(obj['/BitsPerComponent']),
            'filters':filters,'declared_raster_bytes':declared,'declared_raster_bytes_validity':'unverified','jpeg':jpeg,
            'dictionary':{str(k):_record(v,state) for k,v in obj.items()}}

def _font(font,state):
    from pypdf.generic import NameObject,TextStringObject,ByteStringObject
    if not isinstance(font,dict) or not isinstance(font.get('/Subtype'),NameObject) or font.get('/Subtype') not in ('/Type1','/TrueType','/Type0'):fail('unsupported_font_declaration')
    allowed={'/Type','/Subtype','/BaseFont','/FirstChar','/LastChar','/Widths','/FontDescriptor','/Encoding','/ToUnicode','/Name','/DescendantFonts'}
    if not set(font)<=allowed or not isinstance(font.get('/BaseFont'),NameObject):fail('unsupported_font_dependency')
    if '/Type' in font and (not isinstance(font['/Type'],NameObject) or font['/Type']!='/Font'):fail('invalid_font_type')
    if any(k in font for k in ('/FirstChar','/LastChar','/Widths')):
        first,last=font.get('/FirstChar'),font.get('/LastChar');widths=native._object(font.get('/Widths'))
        if not integer(first) or not integer(last) or not first<=last<=255 or not native._numbers(widths,last-first+1):fail('invalid_font_widths')
    encoding=native._object(font.get('/Encoding'))
    if encoding is not None and not isinstance(encoding,(NameObject,dict)):fail('invalid_font_encoding')
    if isinstance(encoding,dict) and not hasattr(encoding,'_data'):
        if not set(encoding)<={'/Type','/BaseEncoding','/Differences'}:fail('unsupported_font_encoding')
        if '/BaseEncoding' in encoding and not isinstance(encoding['/BaseEncoding'],NameObject):fail('invalid_font_encoding')
        differences=native._object(encoding.get('/Differences',[]));code=None
        if not isinstance(differences,list):fail('invalid_font_encoding')
        for value in differences:
            if integer(value) and value<=255:code=int(value)
            elif isinstance(value,NameObject) and code is not None and code<=255:code+=1
            else:fail('invalid_font_encoding')
    if '/ToUnicode' in font and not hasattr(native._object(font['/ToUnicode']),'_data'):fail('invalid_font_cmap_stream')
    descriptor=native._object(font.get('/FontDescriptor'))
    if font['/Subtype']=='/Type0':
        if encoding not in ('/Identity-H','/Identity-V') and not hasattr(encoding,'_data'):fail('unsupported_composite_encoding')
        descendants=native._object(font.get('/DescendantFonts'))
        if not isinstance(descendants,list) or len(descendants)!=1:fail('invalid_font_descendants')
        child=native._object(descendants[0])
        allowed_child={'/Type','/Subtype','/BaseFont','/CIDSystemInfo','/FontDescriptor','/DW','/W','/CIDToGIDMap'}
        if not isinstance(child,dict) or not set(child)<=allowed_child or not isinstance(child.get('/Subtype'),NameObject) or child.get('/Subtype') not in ('/CIDFontType0','/CIDFontType2'):fail('unsupported_cidfont_declaration')
        if not isinstance(child.get('/BaseFont'),NameObject) or ('/Type' in child and (not isinstance(child['/Type'],NameObject) or child['/Type']!='/Font')):fail('invalid_cidfont_declaration')
        info=native._object(child.get('/CIDSystemInfo'))
        if not isinstance(info,dict) or set(info)!={'/Registry','/Ordering','/Supplement'} or not integer(info['/Supplement']):fail('invalid_cid_system_info')
        if any(not isinstance(info[k],(TextStringObject,ByteStringObject)) for k in ('/Registry','/Ordering')):fail('invalid_cid_system_info')
        if '/DW' in child and not native._numbers([child['/DW']]):fail('invalid_cid_width')
        widths=native._object(child.get('/W',[]));offset=0
        if not isinstance(widths,list):fail('invalid_cid_width')
        while offset<len(widths):
            native._pulse(state['check']);first=widths[offset];offset+=1
            if not integer(first) or first>65535 or offset>=len(widths):fail('invalid_cid_width')
            value=native._object(widths[offset]);offset+=1
            if isinstance(value,list):
                if not value or not native._numbers(value) or first+len(value)>65536:fail('invalid_cid_width')
            else:
                if not integer(value) or not first<=value<=65535 or offset>=len(widths) or not native._numbers([widths[offset]]):fail('invalid_cid_width')
                offset+=1
        mapping=native._object(child.get('/CIDToGIDMap','/Identity'))
        if mapping!='/Identity' and not hasattr(mapping,'_data'):fail('invalid_cid_map')
        descriptor=native._object(child.get('/FontDescriptor'))
        if descriptor is None:fail('invalid_font_descriptor')
    elif '/DescendantFonts' in font:fail('invalid_font_descendants')
    if descriptor is not None:
        allowed_descriptor={'/Type','/FontName','/FontFamily','/FontStretch','/FontWeight','/Flags','/FontBBox','/ItalicAngle','/Ascent','/Descent','/Leading','/CapHeight','/XHeight','/StemV','/StemH','/AvgWidth','/MaxWidth','/MissingWidth','/FontFile','/FontFile2','/FontFile3','/CharSet'}
        if not isinstance(descriptor,dict) or not set(descriptor)<=allowed_descriptor:fail('unsupported_font_descriptor')
        if '/FontBBox' in descriptor and not native._numbers(native._object(descriptor['/FontBBox']),4):fail('invalid_font_descriptor')
        if '/Flags' in descriptor and not integer(descriptor['/Flags']):fail('invalid_font_descriptor')
        if '/FontName' in descriptor and not isinstance(descriptor['/FontName'],NameObject):fail('invalid_font_descriptor')
        for key in ('/FontFile','/FontFile2','/FontFile3'):
            if key in descriptor and not hasattr(native._object(descriptor[key]),'_data'):fail('invalid_font_program_stream')
    return _record(font,state)

def _resources(resources,state,active=()):
    """Every listed rendering resource is inventoried, including unused entries."""
    from pypdf.generic import NameObject
    resources=native._object(resources)
    if not isinstance(resources,dict):fail('invalid_resources')
    if not set(resources)<={'/Font','/XObject','/ExtGState','/ColorSpace','/Properties','/ProcSet'}:fail('unsupported_resource_category')
    result={};images={}
    for category,entries in resources.items():
        entries=native._object(entries)
        if category=='/ProcSet':
            if not isinstance(entries,list) or any(not isinstance(v,NameObject) or v not in ('/PDF','/Text','/ImageB','/ImageC','/ImageI') for v in entries):fail('invalid_procset')
            result[category]=list(map(str,entries));continue
        if not isinstance(entries,dict):fail('invalid_resource_dictionary')
        result[category]={}
        for name,reference in entries.items():
            native._pulse(state['check']);obj=native._object(reference)
            if category=='/Font':receipt=_font(obj,state)
            elif category=='/XObject':
                if not isinstance(obj,dict):fail('invalid_xobject')
                if obj.get('/Subtype')=='/Image':receipt=_image(obj,resources,state);images[str(name)]=receipt
                elif obj.get('/Subtype')=='/Form':
                    if not isinstance(obj['/Subtype'],NameObject) or ('/Type' in obj and (not isinstance(obj['/Type'],NameObject) or obj['/Type']!='/XObject')):fail('invalid_form_type')
                    if id(obj) in active or len(active)>=32:fail('form_cycle_or_depth')
                    if not native._numbers(native._object(obj.get('/BBox')),4) or ('/Matrix' in obj and not native._numbers(native._object(obj['/Matrix']),6)):fail('form_geometry')
                    # Contents are parsed on invocation. Uninvoked forms are
                    # independently parsed in inventory mode as well.
                    receipt={'form':_stream(obj,resources,state,None,(*active,id(obj)),inventory_only=True)}
                else:fail('unsupported_xobject')
            elif category=='/ExtGState':native._ext_gstate(obj);receipt=_record(obj,state)
            elif category=='/ColorSpace':native._color_components(obj,resources);receipt=_record(obj,state)
            else:receipt=_record(obj,state)
            result[category][str(name)]=receipt
    return result,images

def _content(stream,state,form):
    stream=native._object(stream)
    refs=stream if isinstance(stream,list) else ([] if stream is None else [stream])
    blocks=[];receipts=[]
    for reference in refs:
        obj=native._object(reference)
        if not isinstance(obj,dict):fail('content_stream')
        allowed={'/Length','/Filter','/DecodeParms'}|({'/Type','/Subtype','/BBox','/Matrix','/Resources','/FormType'} if form else set())
        if not set(obj)<=allowed:fail('unsupported_content_dependency')
        if form and '/FormType' in obj and obj['/FormType']!=1:fail('form_type')
        data=_decode_stream(obj,state,'form' if form else 'page')
        receipts.append({'kind':'form' if form else 'page','encoded_sha256':native._sha(obj._data),'encoded_bytes':len(obj._data),
                         'decoded_sha256':native._sha(data),'decoded_bytes':len(data)})
        blocks.append(data)
    # Interpret the exact concatenation, inserting no source bytes. Each
    # component stream's original and decoded hashes remain separately bound.
    return b''.join(blocks),receipts

def _stream(stream,resources,state,page_box,active=(),inventory_only=False,inherited=None):
    from pypdf.generic import NameObject,TextStringObject,ByteStringObject
    obj=native._object(stream)
    if active and isinstance(obj,dict):resources=native._object(obj.get('/Resources',resources))
    data,streams=_content(stream,state,bool(active))
    resource_receipt,images=_resources(resources,state,active)
    ops=[];invocations=[];depth=0;marked=0;compat=0;stack=[];colors=[1,1]
    cm=tuple(inherited or (1.,0.,0.,1.,0.,0.));font=None
    for space in native._DEVICE_COLORS:native._color_components(NameObject(space),resources)
    for index,(operands,operator,start,end) in enumerate(native._content_operations(data,state['check'])):
        if operator not in native._KNOWN:fail('unknown_operator')
        if operator in native._ZERO_OPERANDS and operands:fail('operator_operands')
        if operator in native._NUMERIC_OPERANDS and not native._numbers(operands,native._NUMERIC_OPERANDS[operator]):fail('operator_operands')
        native._visual_operator(operator,operands,resources,colors)
        receipt={'operator_index':index,'operator':operator.decode(),'operands':[_record(v,state) for v in operands],
                 'ctm':list(cm),'source_byte_start':start,'source_byte_end':end,'content_sha256':native._sha(data)}
        _append(state,ops,receipt)
        if operator==b'q':stack.append((cm,font,colors[:]))
        elif operator==b'Q':
            if not stack:fail('unbalanced_graphics')
            cm,font,colors=stack.pop()
        elif operator==b'cm':cm=native._matrix(tuple(map(float,operands)),cm)
        elif operator==b'BT':
            if depth:fail('unbalanced_text')
            depth=1
        elif operator==b'ET':
            if not depth:fail('unbalanced_text')
            depth=0
        elif operator==b'Tf':
            if len(operands)!=2 or not isinstance(operands[0],NameObject) or not native._numbers([operands[1]]) or operands[1]<=0:fail('font_operand')
            native._resource(resources,'/Font',operands[0]);font=str(operands[0])
        elif operator in native._TEXT:
            if not depth or font is None:fail('text_without_font')
            strings=(TextStringObject,ByteStringObject)
            if operator==b'TJ':
                if len(operands)!=1 or not isinstance(operands[0],list) or any(not isinstance(v,(*strings,int,float)) for v in operands[0]):fail('text_operands')
            elif operator==b'"':
                if len(operands)!=3 or not native._numbers(operands[:2]) or not isinstance(operands[2],strings):fail('text_operands')
            elif len(operands)!=1 or not isinstance(operands[0],strings):fail('text_operands')
        elif operator==b'Tr' and (not integer(operands[0]) or operands[0]>7):fail('text_render_mode')
        elif operator in (b'BMC',b'BDC'):marked+=1
        elif operator==b'EMC':
            marked-=1
            if marked<0:fail('unbalanced_marked_content')
        elif operator==b'BX':compat+=1
        elif operator==b'EX':
            compat-=1
            if compat<0:fail('unbalanced_compatibility')
        elif operator==b'Do':
            xobj=native._resource(resources,'/XObject',operands[0]);name=str(operands[0])
            if xobj.get('/Subtype')=='/Image':
                proof=images[name]
                corners=[(cm[0]*x+cm[2]*y+cm[4],cm[1]*x+cm[3]*y+cm[5]) for x,y in ((0,0),(1,0),(1,1),(0,1))]
                area=abs(cm[0]*cm[3]-cm[1]*cm[2])
                if not math.isfinite(area) or any(not math.isfinite(v) for p in corners for v in p):fail('image_geometry')
                box=[min(p[0] for p in corners),min(p[1] for p in corners),max(p[0] for p in corners),max(p[1] for p in corners)]
                intersects=bool(page_box and area>0 and _intersects(corners,page_box))
                _append(state,invocations,{'name':name,'operator_index':index,'ctm':list(cm),'bounds':box,
                    'positive_area_page_intersection':intersects,'visibility':'unverified',**proof})
            else:
                if id(xobj) in active or len(active)>=32:fail('form_cycle_or_depth')
                form_cm=native._matrix(tuple(map(float,xobj.get('/Matrix',(1,0,0,1,0,0)))),cm)
                nested=_stream(xobj,resources,state,page_box,(*active,id(xobj)),inherited=form_cm)
                _append(state,invocations,{'name':name,'operator_index':index,'form':nested})
    if depth or marked or compat or stack:fail('unbalanced_content')
    result={'streams':streams,'resources':resource_receipt,'operations':ops,'image_invocations':invocations,
            'content_sha256':native._sha(data),'content_bytes':len(data)}
    if active:
        result['form_geometry']={'bbox':list(map(float,obj['/BBox'])),
            'matrix':list(map(float,obj.get('/Matrix',(1,0,0,1,0,0)))),
            'form_type':int(obj.get('/FormType',1))}
    # Resource dictionaries also consume the single document provenance bound.
    charged=[];_append(state,charged,{'streams':streams,'resources':resource_receipt})
    return result

def _intersects(corners, box):
    """Clip the ordered transformed unit square against the page rectangle."""
    polygon=corners
    for axis,boundary,greater in ((0,box[0],True),(0,box[2],False),(1,box[1],True),(1,box[3],False)):
        if not polygon:return False
        output=[];previous=polygon[-1]
        inside=lambda p: p[axis]>=boundary if greater else p[axis]<=boundary
        for current in polygon:
            before,after=inside(previous),inside(current)
            if before!=after:
                denominator=current[axis]-previous[axis]
                fraction=(boundary-previous[axis])/denominator
                point=tuple(previous[j]+fraction*(current[j]-previous[j]) for j in (0,1))
                if not all(math.isfinite(v) for v in point):fail('image_geometry')
                output.append(point)
            if after:output.append(current)
            previous=current
        polygon=output
    if len(polygon)<3:return False
    area=sum(polygon[i][0]*polygon[(i+1)%len(polygon)][1]-polygon[(i+1)%len(polygon)][0]*polygon[i][1] for i in range(len(polygon)))
    if not math.isfinite(area):fail('image_geometry')
    return abs(area)>0

def _presence(value):
    return any(row.get('positive_area_page_intersection',False) or ('form' in row and _presence(row['form'])) for row in value['image_invocations'])

def _declaration(value,state,depth=0):
    """Record non-rendering declarations without interpreting linked tag trees.

    Indirect reference identities are retained rather than expanding semantic
    backlinks to page/parent objects. The original PDF digest binds every byte;
    this canonical declaration digest is not a claim to validate tag meaning.
    """
    from pypdf.generic import IndirectObject
    native._pulse(state['check']);_reserve(state,2)
    if depth>32:fail('declaration_depth')
    if isinstance(value,IndirectObject):
        if not integer(value.idnum,1) or not integer(value.generation):fail('declaration_reference')
        return {'object_number':int(value.idnum),'generation':int(value.generation)}
    if isinstance(value,list):return [_declaration(v,state,depth+1) for v in value]
    if isinstance(value,dict):
        if hasattr(value,'_data'):fail('unsupported_tag_stream')
        result={}
        for key,item in value.items():
            _reserve(state,len(str(key))*6+3)
            result[str(key)]=_declaration(item,state,depth+1)
        return result
    return _record(value,state)

def _catalog(root,state):
    from pypdf.generic import NameObject,BooleanObject,TextStringObject,ByteStringObject
    allowed={'/Type','/Pages','/Metadata','/Lang','/MarkInfo','/StructTreeRoot','/ViewerPreferences','/AcroForm'}
    if not set(root)<=allowed:fail('unsupported_catalog_dependency')
    if not isinstance(root.get('/Type'),NameObject) or root['/Type']!='/Catalog':fail('invalid_catalog_type')
    declarations={'/Type':'/Catalog'}
    for key in sorted(set(root)-{'/Type','/Pages'}):
        value=native._object(root[key])
        if key=='/Metadata':
            if not isinstance(value,dict) or not hasattr(value,'_data') or not set(value)<={'/Type','/Subtype','/Length','/Filter','/DecodeParms'}:fail('invalid_metadata_stream')
            if not isinstance(value.get('/Type'),NameObject) or value['/Type']!='/Metadata' or not isinstance(value.get('/Subtype'),NameObject) or value['/Subtype']!='/XML':fail('invalid_metadata_stream')
            data=_decode_stream(value,state,'metadata')
            declarations[key]={'dictionary':{str(k):_record(v,state) for k,v in value.items()},
                'encoded_sha256':native._sha(value._data),'encoded_bytes':len(value._data),
                'decoded_sha256':native._sha(data),'decoded_bytes':len(data)}
        elif key=='/Lang':
            if not isinstance(value,(TextStringObject,ByteStringObject)):fail('invalid_document_language')
            declarations[key]=_record(value,state)
        elif key in ('/MarkInfo','/ViewerPreferences'):
            allowed_keys={'/Marked','/UserProperties','/Suspects'} if key=='/MarkInfo' else {'/DisplayDocTitle'}
            if not isinstance(value,dict) or not set(value)<=allowed_keys or any(not isinstance(native._object(v),BooleanObject) for v in value.values()):fail('unsupported_catalog_declaration')
            declarations[key]=_record(value,state)
        elif key=='/StructTreeRoot':
            allowed_keys={'/Type','/K','/ParentTree','/RoleMap','/ParentTreeNextKey','/ClassMap','/IDTree'}
            if not isinstance(value,dict) or not set(value)<=allowed_keys or not isinstance(value.get('/Type'),NameObject) or value['/Type']!='/StructTreeRoot':fail('invalid_structure_declaration')
            if '/ParentTreeNextKey' in value and not integer(value['/ParentTreeNextKey']):fail('invalid_structure_declaration')
            for name in ('/ParentTree','/RoleMap','/ClassMap','/IDTree'):
                if name in value and not isinstance(native._object(value[name]),dict):fail('invalid_structure_declaration')
            declaration=_declaration(value,state)
            packed=json.dumps(declaration,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode()
            declarations[key]={'declaration':declaration,'declaration_sha256':native._sha(packed),'semantics':'unverified'}
        else:
            # The existing document validator admits only an empty AcroForm.
            declarations[key]=_record(value,state)
    result={'metadata_semantics':'unverified','tagging_semantics':'unverified','declarations':declarations}
    charged=[];_append(state,charged,result)
    return result

def extract_pdf_visual_evidence(body:bytes,*,max_pdf_bytes:int,max_pages:int,max_stream_bytes:int,check=None)->dict:
    """Complete structural receipt or technical error, never a fallback/recovery."""
    if any(type(v) is not int or v<=0 for v in (max_pdf_bytes,max_pages,max_stream_bytes)):fail('invalid_limits')
    try:
        decoded,encoding=native._decode(body,max_pdf_bytes,check)
        import pypdf
        from pypdf import PdfReader,apply_configuration
        from pypdf.errors import LimitReachedError
        state={'limit':max_stream_bytes,'decoded_stream_bytes':0,'inventory_bytes':0,'check':check}
        caps=dict(maximum_declared_stream_length=max_stream_bytes,array_based_stream_maximum_output_length=max_stream_bytes,
            zlib_maximum_output_length=max_stream_bytes,zlib_maximum_recovery_input_length=0,stream_decoding_work_maximum_length=max_stream_bytes,
            image_maximum_buffer_size=max_stream_bytes,page_tree_maximum_entries=max_pages*4+100,page_tree_maximum_depth=100)
        with apply_configuration(**caps),native._diagnostics() as notices:
            try:
                reader=PdfReader(io.BytesIO(decoded),strict=True)
                if reader.is_encrypted:fail('encrypted')
                count=native._page_tree(reader,max_pages,check)
                if len(reader.pages)!=count:fail('page_count_mismatch')
                native._document_findings(reader.root_object)
                document_declarations=_catalog(reader.root_object,state)
                from pypdf.generic import NameObject
                pages=[]
                for index,page in enumerate(reader.pages):
                    native._pulse(check)
                    allowed_page={'/Type','/Parent','/Resources','/Contents','/MediaBox','/CropBox','/Rotate','/Group','/UserUnit','/StructParents','/Tabs'}
                    if not set(page)<=allowed_page:fail('unsupported_page_dependency')
                    if not isinstance(page.get('/Type'),NameObject) or page['/Type']!='/Page':fail('invalid_page_type')
                    user_unit=native._object(page.get('/UserUnit',1))
                    if not native._numbers([user_unit]) or not 0<user_unit<=75000:fail('page_user_unit')
                    if '/StructParents' in page and not integer(page['/StructParents']):fail('page_structure_parent')
                    if '/Tabs' in page and (not isinstance(page['/Tabs'],NameObject) or page['/Tabs'] not in ('/R','/C','/S')):fail('page_tabs')
                    if native._annotation_inventory(page):fail('unsupported_annotation')
                    resources=native._object(page.get('/Resources',{}))
                    if '/Group' in page:native._page_group(page['/Group'],resources)
                    rotation=native._object(page.get('/Rotate',0))
                    if not isinstance(rotation,int) or isinstance(rotation,bool) or rotation%90:fail('page_rotation')
                    media=native._object(page.get('/MediaBox'))
                    crop=native._object(page.get('/CropBox',media))
                    # Page convenience properties coerce string values into
                    # numbers. Validate the original inherited arrays first.
                    if not native._numbers(media,4) or not native._numbers(crop,4) or media[0]>=media[2] or media[1]>=media[3] or crop[0]>=crop[2] or crop[1]>=crop[3]:fail('page_geometry')
                    media=list(map(float,media));crop=list(map(float,crop))
                    row=_stream(page.get('/Contents'),resources,state,crop)
                    row.update(page_number=index+1,page_geometry={'media_box':media,'crop_box':crop,'rotation':int(rotation),'user_unit':float(user_unit),
                        'group':_record(page.get('/Group'),state),
                        'struct_parents':int(page['/StructParents']) if '/StructParents' in page else None,
                        'tabs':str(page['/Tabs']) if '/Tabs' in page else None})
                    pages.append(row)
                if notices:fail('parser_recovery_warning')
                return {'version':VERSION,'schema_version':1,'library':'pypdf','library_version':pypdf.__version__,
                    'input_sha256':native._sha(body),'input_bytes':len(body),'decoded_sha256':native._sha(decoded),'decoded_bytes':len(decoded),'encoding':encoding,
                    'page_count':count,'pages':pages,'document_declarations':document_declarations,'decoded_stream_bytes':state['decoded_stream_bytes'],'inventory_bytes':state['inventory_bytes'],
                    'visual_presence':any(_presence(p) for p in pages),'native_text_complete':False,'encoded_image_validity':'unverified',
                    'visual_meaning':'unresolved','native_unicode':'unverified','text_visibility':'unverified','order_authority':False}
            except LimitReachedError:fail('stream_or_structure_limit')
            except FilingPDFVisualError:raise
            except native.FilingPDFError as error:raise FilingPDFVisualError(str(error)) from None
            except Exception:fail('malformed_structure')
    except native._Cancelled as cancelled:raise cancelled.error from None
    except native.FilingPDFError as error:raise FilingPDFVisualError(str(error)) from None


def validate_pdf_visual_receipt(receipt):
    """Validate shape/limited claims only; authenticity requires raw re-extraction."""
    import re
    required={'version','schema_version','library','library_version','input_sha256','input_bytes',
        'decoded_sha256','decoded_bytes','encoding','page_count','pages','document_declarations','decoded_stream_bytes','inventory_bytes',
        'visual_presence','native_text_complete','encoded_image_validity','visual_meaning','native_unicode','text_visibility','order_authority'}
    def require(condition):
        if not condition:fail('invalid_receipt')
    def digest(value):return isinstance(value,str) and re.fullmatch(r'[0-9a-f]{64}',value) is not None
    def finite(values,count):return isinstance(values,list) and len(values)==count and all(type(v) in (int,float) and math.isfinite(v) for v in values)
    def walk(value,depth=0):
        require(depth<=64)
        if isinstance(value,dict):
            for key,item in value.items():
                require(isinstance(key,str))
                if key.endswith('sha256'):require(digest(item))
                if key in ('entropy_validation','pixel_validation'):require(item=='not_performed')
                if key in ('declared_raster_bytes_validity','visibility'):require(item=='unverified')
                walk(item,depth+1)
        elif isinstance(value,list):
            for item in value:walk(item,depth+1)
        else:require(value is None or type(value) in (str,bool,int) or type(value) is float and math.isfinite(value))
    def content(value,page=False):
        keys={'streams','resources','operations','image_invocations','content_sha256','content_bytes'}
        if page:keys|={'page_number','page_geometry'}
        elif isinstance(value,dict) and 'form_geometry' in value:
            keys.add('form_geometry')
            geometry=value['form_geometry']
            require(isinstance(geometry,dict) and set(geometry)=={'bbox','matrix','form_type'}
                and finite(geometry['bbox'],4) and finite(geometry['matrix'],6)
                and type(geometry['form_type']) is int and geometry['form_type']==1)
        require(isinstance(value,dict) and set(value)==keys)
        require(digest(value['content_sha256']) and integer(value['content_bytes']))
        require(isinstance(value['resources'],dict) and isinstance(value['streams'],list))
        for row in value['streams']:
            require(isinstance(row,dict) and set(row)=={'kind','encoded_sha256','encoded_bytes','decoded_sha256','decoded_bytes'})
            require(row['kind'] in ('page','form') and integer(row['encoded_bytes']) and integer(row['decoded_bytes']))
        require(isinstance(value['operations'],list))
        for index,row in enumerate(value['operations']):
            require(isinstance(row,dict) and set(row)=={'operator_index','operator','operands','ctm','source_byte_start','source_byte_end','content_sha256'})
            require(type(row['operator_index']) is int and row['operator_index']==index and isinstance(row['operands'],list))
            require(isinstance(row['operator'],str) and row['operator'].encode() in native._KNOWN and finite(row['ctm'],6))
            require(integer(row['source_byte_start']) and integer(row['source_byte_end']) and row['source_byte_start']<row['source_byte_end']<=value['content_bytes'])
            require(row['content_sha256']==value['content_sha256'])
        require(isinstance(value['image_invocations'],list))
        for row in value['image_invocations']:
            require(isinstance(row,dict) and isinstance(row.get('name'),str) and integer(row.get('operator_index')))
            require(row['operator_index']<len(value['operations']) and value['operations'][row['operator_index']]['operator']=='Do')
            if 'form' in row:
                require(set(row)=={'name','operator_index','form'});content(row['form'])
            else:
                require(set(row)=={'name','operator_index','ctm','bounds','positive_area_page_intersection','visibility',
                    'encoded_sha256','encoded_bytes','decoded_sha256','decoded_bytes','width','height','components','bits','filters',
                    'declared_raster_bytes','declared_raster_bytes_validity','jpeg','dictionary'})
                require(finite(row['ctm'],6) and finite(row['bounds'],4) and type(row['positive_area_page_intersection']) is bool)
                for key in ('encoded_bytes','decoded_bytes','width','height','components','bits','declared_raster_bytes'):require(integer(row[key],1))
                require(isinstance(row['dictionary'],dict) and isinstance(row['filters'],list))
                if row['jpeg'] is not None:
                    jpg=row['jpeg']
                    require(isinstance(jpg,dict) and set(jpg)=={'frame','segments','scans','framing','entropy_validation','pixel_validation','sha256','bytes'})
                    require(jpg['framing']=='complete' and jpg['entropy_validation']==jpg['pixel_validation']=='not_performed')
                    require(isinstance(jpg['frame'],dict) and isinstance(jpg['segments'],list) and isinstance(jpg['scans'],list) and bool(jpg['scans']))
                    require(jpg['sha256']==row['decoded_sha256'] and jpg['bytes']==row['decoded_bytes'])
    try:
        require(isinstance(receipt,dict) and set(receipt)==required)
        require(receipt['version']==VERSION and type(receipt['schema_version']) is int and receipt['schema_version']==1)
        require(receipt['library']=='pypdf' and isinstance(receipt['library_version'],str) and bool(receipt['library_version']))
        require(receipt['native_text_complete'] is False and receipt['order_authority'] is False)
        require(receipt['encoded_image_validity']==receipt['native_unicode']==receipt['text_visibility']=='unverified')
        require(receipt['visual_meaning']=='unresolved' and type(receipt['visual_presence']) is bool)
        require(receipt['encoding'] in ('pdf','sec_pdf','sec_pdf_uu','uu'))
        declarations=receipt['document_declarations']
        require(isinstance(declarations,dict) and set(declarations)=={'metadata_semantics','tagging_semantics','declarations'})
        require(declarations['metadata_semantics']==declarations['tagging_semantics']=='unverified' and isinstance(declarations['declarations'],dict))
        for key in ('input_bytes','decoded_bytes','page_count'):require(integer(receipt[key],1))
        for key in ('decoded_stream_bytes','inventory_bytes'):require(integer(receipt[key]))
        require(isinstance(receipt['pages'],list) and len(receipt['pages'])==receipt['page_count'])
        for index,page in enumerate(receipt['pages']):
            content(page,True)
            require(type(page['page_number']) is int and page['page_number']==index+1)
            geometry=page['page_geometry']
            require(isinstance(geometry,dict) and set(geometry)=={'media_box','crop_box','rotation','user_unit','group','struct_parents','tabs'})
            require(finite(geometry['media_box'],4) and finite(geometry['crop_box'],4) and type(geometry['rotation']) is int and geometry['rotation']%90==0)
            require(type(geometry['user_unit']) in (int,float) and math.isfinite(geometry['user_unit']) and 0<geometry['user_unit']<=75000)
            require(geometry['group'] is None or isinstance(geometry['group'],dict))
            require(geometry['struct_parents'] is None or integer(geometry['struct_parents']))
            require(geometry['tabs'] in (None,'/R','/C','/S'))
        require(receipt['visual_presence']==any(_presence(page) for page in receipt['pages']))
        walk(receipt)
        return True
    except FilingPDFVisualError:raise
    except (KeyError,TypeError,ValueError,OverflowError,RecursionError):fail('invalid_receipt')
