"""Bounded native PDF evidence, without acquisition, OCR or disposition decisions.

Call inside the existing resource-limited parser process: pypdf calls cannot be
preempted by a Python callback. A successful inventory is not proof of complete
visual meaning. ``supported_text_layout`` certifies only the narrow plain-text
operator/geometry subset documented in ``_plain_layout``; all other valid PDFs
retain observations and capability limits separately.
"""
from __future__ import annotations

import binascii
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import io
import json
import logging
import math
import re
import zlib

VERSION = 'sec-native-pdf-evidence-v3'
_TEXT = {b'Tj', b'TJ', b"'", b'"'}
_PAINT = {b'S', b's', b'f', b'F', b'f*', b'B', b'B*', b'b', b'b*', b'sh'}
_PLAIN = {b'BT', b'ET', b'Tf', b'Td', b'TD', b'Tm', b'T*', b'TL', b'Tj'}
_KNOWN = _PLAIN | _TEXT | _PAINT | {
    b'q', b'Q', b'cm', b'w', b'J', b'j', b'M', b'd', b'ri', b'i', b'gs',
    b'm', b'l', b'c', b'v', b'y', b'h', b're', b'n', b'W', b'W*',
    b'CS', b'cs', b'SC', b'SCN', b'sc', b'scn', b'G', b'g', b'RG', b'rg', b'K', b'k',
    b'Tc', b'Tw', b'Tz', b'Tr', b'Ts', b'Do', b'INLINE IMAGE',
    b'MP', b'DP', b'BMC', b'BDC', b'EMC', b'BX', b'EX', b'd0', b'd1',
}
_STANDARD = {'/Helvetica', '/Helvetica-Bold', '/Helvetica-Oblique', '/Helvetica-BoldOblique',
             '/Times-Roman', '/Times-Bold', '/Times-Italic', '/Times-BoldItalic',
             '/Courier', '/Courier-Bold', '/Courier-Oblique', '/Courier-BoldOblique'}
_ACTIVE_LOG = ContextVar('filing_pdf_log', default=None)
_ZERO_OPERANDS = {b'BT', b'ET', b'q', b'Q', b'h', b'n', b'S', b's', b'f', b'F',
                  b'f*', b'B', b'B*', b'b', b'b*', b'W', b'W*', b'T*', b'EMC', b'BX', b'EX'}
_NUMERIC_OPERANDS = {**dict.fromkeys((b'm', b'l', b'Td', b'TD', b'd0'), 2),
    **dict.fromkeys((b'c', b'cm', b'Tm', b'd1'), 6),
    **dict.fromkeys((b'v', b'y', b're', b'K', b'k'), 4),
    **dict.fromkeys((b'RG', b'rg'), 3),
    **dict.fromkeys((b'w', b'J', b'j', b'M', b'i', b'Tc', b'Tw', b'Tz', b'TL',
                     b'Tr', b'Ts', b'G', b'g'), 1)}


class FilingPDFError(ValueError):
    """Fixed safe technical diagnostic; never an allowed deferral reason."""


class _Cancelled(BaseException):
    def __init__(self, error):
        self.error = error


def _pulse(check):
    if check is not None:
        try:
            check()
        except Exception as error:
            # Some library paths catch Exception. Cancellation must escape them.
            raise _Cancelled(error) from None


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _decode(body, limit, check):
    _pulse(check)
    if not isinstance(body, bytes) or not body or len(body) > limit:
        raise FilingPDFError('pdf_byte_limit')
    payload, encoding = body, 'pdf'
    if body.lstrip().startswith(b'<PDF>'):
        match = re.fullmatch(rb'[ \t\r\n]*<PDF>(?:\r\n|\r|\n)(.*?)</PDF>[ \t\r\n]*', body, re.S)
        if match is None:
            raise FilingPDFError('pdf_wrapper_invalid')
        payload, encoding = match[1], 'sec_pdf'
    if payload.startswith(b'begin '):
        lines = payload.splitlines()
        if (len(lines) < 4 or not re.fullmatch(rb'begin [0-7]{3} [^\r\n]+', lines[0])
                or lines[-1] != b'end' or lines[-2] not in ((b'`', b' ', b'') if encoding == 'sec_pdf' else (b'`', b' '))):
            raise FilingPDFError('pdf_uu_invalid')
        decoded = bytearray()
        for line in lines[1:-2]:
            _pulse(check)
            if not line or not 33 <= line[0] <= 77:
                raise FilingPDFError('pdf_uu_invalid')
            count = line[0] - 32
            expected = 1 + 4 * ((count + 2) // 3)
            if ((len(line) > expected or (encoding != 'sec_pdf' and len(line) != expected))
                    or any(not 32 <= byte <= 96 for byte in line)):
                raise FilingPDFError('pdf_uu_invalid')
            try:
                # Captured SEC native submissions trim encoded ASCII-space
                # (zero sextet) suffixes, including the zero-length terminator.
                # Restrict this convention to explicitly framed SEC payloads.
                encoded_line = line
                if encoding == 'sec_pdf':
                    encoded_line = line.ljust(expected, b' ')
                    # Older SEC encoders leave nonzero ignored padding sextets.
                    # Only sextets outside the declared byte count are ignored;
                    # every payload sextet remains unchanged.
                    unused = {0: 0, 1: 2, 2: 1}[count % 3]
                    if unused:
                        encoded_line = encoded_line[:-unused] + b' ' * unused
                block = binascii.a2b_uu(encoded_line)
            except binascii.Error:
                raise FilingPDFError('pdf_uu_invalid') from None
            if len(decoded) + len(block) > limit:
                raise FilingPDFError('pdf_byte_limit')
            decoded.extend(block)
        payload = bytes(decoded)
        encoding = 'sec_pdf_uu' if encoding == 'sec_pdf' else 'uu'
    _pulse(check)
    if (not re.match(rb'%PDF-[12]\.[0-9](?:\r|\n)', payload)
            or not re.search(rb'(?:\r|\n)%%EOF[ \t\r\n]*\Z', payload)):
        raise FilingPDFError('pdf_incomplete_framing')
    return payload, encoding


@contextmanager
def _diagnostics():
    """Fail closed on library recovery warnings, without retaining source text."""
    marker, notices = object(), []
    class Handler(logging.Handler):
        def emit(self, record):
            if _ACTIVE_LOG.get() is marker and record.levelno >= logging.WARNING:
                notices.append(True)
    handler = Handler()
    logger = logging.getLogger('pypdf')
    logger.addHandler(handler)
    token = _ACTIVE_LOG.set(marker)
    try:
        yield notices
    finally:
        _ACTIVE_LOG.reset(token)
        logger.removeHandler(handler)


def _object(value):
    return value.get_object() if hasattr(value, 'get_object') else value


def _page_tree(reader, max_pages, check):
    seen, pages = set(), []
    def visit(reference, depth=0):
        _pulse(check)
        node = _object(reference)
        key = id(node)
        if key in seen or depth > 100:
            raise FilingPDFError('pdf_invalid_page_tree')
        seen.add(key)
        if len(seen) > max_pages * 4 + 100:
            raise FilingPDFError('pdf_page_tree_limit')
        if node.get('/Type') == '/Page':
            pages.append(node)
            if len(pages) > max_pages:
                raise FilingPDFError('pdf_page_limit')
            return 1
        if node.get('/Type') != '/Pages' or '/Kids' not in node:
            raise FilingPDFError('pdf_invalid_page_tree')
        count = node.get('/Count')
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise FilingPDFError('pdf_invalid_page_tree')
        if count > max_pages:
            raise FilingPDFError('pdf_page_limit')
        actual = sum(visit(kid, depth + 1) for kid in node['/Kids'])
        if actual != count:
            raise FilingPDFError('pdf_page_count_mismatch')
        return actual
    visit(reader.root_object['/Pages'])
    if not pages:
        raise FilingPDFError('pdf_empty_document')
    return len(pages)


_DEVICE_COLORS = {'/DeviceGray': 1, '/DeviceRGB': 3, '/DeviceCMYK': 4}
_RENDER_INTENTS = {'/AbsoluteColorimetric', '/RelativeColorimetric', '/Saturation', '/Perceptual'}


def _numbers(values, count=None):
    return (isinstance(values, (list, tuple)) and (count is None or len(values) == count)
            and all(isinstance(value, (int, float)) and math.isfinite(value) for value in values))


def _resource(resources, category, name):
    from pypdf.generic import NameObject
    if not isinstance(name, NameObject):
        raise FilingPDFError('pdf_invalid_resource_operand')
    entries = _object(resources.get(category, {}))
    if not isinstance(entries, dict):
        raise FilingPDFError('pdf_invalid_resource_dictionary')
    if name not in entries:
        raise FilingPDFError('pdf_missing_visual_resource')
    result = _object(entries[name])
    if result is None:
        raise FilingPDFError('pdf_missing_visual_resource')
    return result


def _color_components(value, resources, *, resolved=False):
    """Only device spaces have a complete bounded inventory in this adapter."""
    from pypdf.generic import NameObject
    value = _object(value)
    if not isinstance(value, NameObject):
        raise FilingPDFError('pdf_unsupported_or_invalid_color_space')
    if value in _DEVICE_COLORS:
        entries = _object(resources.get('/ColorSpace', {}))
        if not isinstance(entries, dict):
            raise FilingPDFError('pdf_invalid_resource_dictionary')
        default = {'/DeviceGray': '/DefaultGray', '/DeviceRGB': '/DefaultRGB', '/DeviceCMYK': '/DefaultCMYK'}[value]
        if not resolved and default in entries:
            replacement = _object(entries[default])
            if replacement != value:
                # Device remapping can hide ICC/function resources. No visual
                # exclusion is justified without inventorying that capability.
                raise FilingPDFError('pdf_unsupported_default_color_space')
        return _DEVICE_COLORS[value]
    if resolved:
        raise FilingPDFError('pdf_unsupported_or_invalid_color_space')
    if value == '/Pattern':
        raise FilingPDFError('pdf_unsupported_pattern_color_space')
    return _color_components(_resource(resources, '/ColorSpace', value), resources, resolved=True)


def _dash(value):
    value = _object(value)
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise FilingPDFError('pdf_invalid_dash_pattern')
    array, phase = _object(value[0]), _object(value[1])
    if (not _numbers(array) or any(number < 0 for number in array)
            or (array and not any(array)) or not _numbers([phase]) or phase < 0):
        raise FilingPDFError('pdf_invalid_dash_pattern')


def _ext_gstate(value):
    """Validate simple parameters; unresolved functions/masks/fonts are failures."""
    from pypdf.generic import BooleanObject, NameObject
    if not isinstance(value, dict):
        raise FilingPDFError('pdf_invalid_extgstate')
    for key, reference in value.items():
        item = _object(reference)
        if key == '/Type':
            if not isinstance(item, NameObject) or item != '/ExtGState':
                raise FilingPDFError('pdf_invalid_extgstate')
        elif key in ('/LW', '/ML', '/FL', '/CA', '/ca'):
            if not _numbers([item]) or item < (1 if key == '/ML' else 0):
                raise FilingPDFError('pdf_invalid_extgstate')
            if key in ('/CA', '/ca') and item > 1 or key == '/FL' and item > 100:
                raise FilingPDFError('pdf_invalid_extgstate')
        elif key in ('/LC', '/LJ', '/OPM'):
            if not isinstance(item, int) or item not in range(2 if key == '/OPM' else 3):
                raise FilingPDFError('pdf_invalid_extgstate')
        elif key == '/BM':
            if not isinstance(item, NameObject) or item != '/Normal':
                raise FilingPDFError('pdf_unsupported_extgstate_capability')
        elif key == '/D':
            _dash(item)
        elif key == '/RI':
            if not isinstance(item, NameObject) or item not in _RENDER_INTENTS:
                raise FilingPDFError('pdf_invalid_extgstate')
        elif key in ('/OP', '/op', '/SA', '/AIS', '/TK'):
            if not isinstance(item, BooleanObject):
                raise FilingPDFError('pdf_invalid_extgstate')
        else:
            # Includes Font, SMask, transfer/halftone functions and blend modes.
            raise FilingPDFError('pdf_unsupported_extgstate_capability')


def _properties(value, tag):
    from pypdf.generic import NameObject, TextStringObject
    if not isinstance(value, dict):
        raise FilingPDFError('pdf_invalid_marked_properties')
    for key, reference in value.items():
        item = _object(reference)
        if key == '/MCID':
            if not isinstance(item, int) or isinstance(item, bool) or item < 0:
                raise FilingPDFError('pdf_invalid_marked_properties')
        elif key == '/Lang':
            if not isinstance(item, TextStringObject) or not re.fullmatch(r'[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*', item):
                raise FilingPDFError('pdf_invalid_marked_properties')
        elif tag == '/Artifact' and key == '/Type':
            if not isinstance(item, NameObject) or item not in ('/Pagination', '/Layout', '/Page', '/Background'):
                raise FilingPDFError('pdf_invalid_marked_properties')
        elif tag == '/Artifact' and key == '/Subtype':
            if not isinstance(item, NameObject) or item not in ('/Header', '/Footer', '/Watermark') or value.get('/Type') != '/Pagination':
                raise FilingPDFError('pdf_invalid_marked_properties')
        elif tag == '/Artifact' and key == '/Attached':
            if not isinstance(item, list) or not item or any(not isinstance(v, NameObject) or v not in ('/Top','/Bottom','/Left','/Right') for v in item):
                raise FilingPDFError('pdf_invalid_marked_properties')
        elif tag == '/Artifact' and key == '/BBox':
            # Artifact bounds are source metadata, not replacement text or a
            # clipping instruction. Preserve them in the operator provenance.
            if not _numbers(item, 4) or item[0] > item[2] or item[1] > item[3]:
                raise FilingPDFError('pdf_invalid_marked_properties')
        else:
            raise FilingPDFError('pdf_unsupported_marked_properties')


def _page_group(value, resources):
    from pypdf.generic import NameObject, BooleanObject
    value = _object(value)
    if not isinstance(value, dict) or value.get('/S') != '/Transparency':
        raise FilingPDFError('pdf_unsupported_page_object_capability')
    for key, item in value.items():
        item = _object(item)
        if key == '/Type' and isinstance(item, NameObject) and item == '/Group':
            continue
        if key == '/S' and isinstance(item, NameObject) and item == '/Transparency':
            continue
        if key == '/CS':
            _color_components(item, resources)
            continue
        if key in ('/I','/K') and isinstance(item, BooleanObject) and not item.value:
            continue
        raise FilingPDFError('pdf_unsupported_page_object_capability')


def _visual_operator(operator, operands, resources, colors):
    from pypdf.generic import NameObject
    if operator in (b'sh', b'gs', b'CS', b'cs', b'Do', b'ri', b'MP', b'BMC'):
        if len(operands) != 1 or not isinstance(operands[0], NameObject):
            raise FilingPDFError('pdf_invalid_visual_operands')
    if operator == b'sh':
        shading = _resource(resources, '/Shading', operands[0])
        if not isinstance(shading, dict):
            raise FilingPDFError('pdf_invalid_shading')
        # Shading functions/meshes are not decoded/inventoried here. Merely
        # resolving a named dictionary cannot establish valid visual evidence.
        raise FilingPDFError('pdf_unsupported_shading_capability')
    if operator == b'gs':
        _ext_gstate(_resource(resources, '/ExtGState', operands[0]))
    elif operator in (b'CS', b'cs'):
        colors[0 if operator == b'CS' else 1] = _color_components(operands[0], resources)
    elif operator in (b'SC', b'SCN', b'sc', b'scn'):
        if any(isinstance(value, NameObject) for value in operands):
            # SCN may reference patterns, but this adapter cannot inventory a
            # pattern's paint stream and dependency graph. Missing still fails.
            if operator in (b'SCN', b'scn') and operands and isinstance(operands[-1], NameObject):
                _resource(resources, '/Pattern', operands[-1])
            raise FilingPDFError('pdf_unsupported_or_invalid_color_operand')
        if not _numbers(operands, colors[0 if operator[:1] == b'S' else 1]):
            raise FilingPDFError('pdf_invalid_color_operands')
    elif operator in (b'G', b'g', b'RG', b'rg', b'K', b'k'):
        space = {b'G': '/DeviceGray', b'g': '/DeviceGray', b'RG': '/DeviceRGB',
                 b'rg': '/DeviceRGB', b'K': '/DeviceCMYK', b'k': '/DeviceCMYK'}[operator]
        colors[0 if operator[:1].isupper() else 1] = _color_components(NameObject(space), resources)
    elif operator in (b'J', b'j') and (not isinstance(operands[0], int) or operands[0] not in range(3)):
        raise FilingPDFError('pdf_invalid_graphics_state_parameter')
    elif operator == b'w' and operands[0] < 0 or operator == b'M' and operands[0] < 1:
        raise FilingPDFError('pdf_invalid_graphics_state_parameter')
    elif operator == b'i' and not 0 <= operands[0] <= 100:
        raise FilingPDFError('pdf_invalid_graphics_state_parameter')
    elif operator == b'd':
        _dash(operands)
    elif operator == b'ri' and operands[0] not in _RENDER_INTENTS:
        raise FilingPDFError('pdf_invalid_rendering_intent')
    elif operator in (b'DP', b'BDC'):
        if len(operands) != 2 or not isinstance(operands[0], NameObject):
            raise FilingPDFError('pdf_invalid_marked_properties')
        value = operands[1]
        if isinstance(value, NameObject):
            value = _resource(resources, '/Properties', value)
        _properties(value, operands[0])
    elif operator in (b'd0', b'd1'):
        raise FilingPDFError('pdf_unsupported_type3_glyph_capability')


def _image_components(image, resources):
    from pypdf.generic import NameObject, BooleanObject
    allowed = {'/Type','/Subtype','/Width','/Height','/BitsPerComponent','/ColorSpace','/Filter','/DecodeParms',
               '/Decode','/Length','/ImageMask','/Interpolate','/Intent'}
    if not set(image) <= allowed:
        raise FilingPDFError('pdf_unsupported_image_dependency')
    if '/ImageMask' in image:
        mask = _object(image['/ImageMask'])
        if not isinstance(mask, BooleanObject) or mask.value:
            raise FilingPDFError('pdf_unsupported_or_invalid_image_mask')
    if '/Interpolate' in image and not isinstance(image['/Interpolate'],BooleanObject):
        raise FilingPDFError('pdf_invalid_image_interpolation')
    if '/Intent' in image and image['/Intent'] not in _RENDER_INTENTS:
        raise FilingPDFError('pdf_invalid_image_rendering_intent')
    bits = image.get('/BitsPerComponent')
    if not isinstance(bits, int) or bits not in (1, 2, 4, 8, 16):
        raise FilingPDFError('pdf_invalid_image_bit_depth')
    count = _color_components(image.get('/ColorSpace'), resources)
    filters = _object(image.get('/Filter', []))
    filters = [filters] if isinstance(filters, NameObject) else filters
    allowed = {'/FlateDecode', '/Fl', '/ASCIIHexDecode', '/AHx', '/ASCII85Decode', '/A85',
               '/LZWDecode', '/LZW', '/RunLengthDecode', '/RL'}
    if not isinstance(filters, list) or any(not isinstance(value, NameObject) or value not in allowed for value in filters):
        # pypdf get_data returns JPEG/JPX payload bytes without proving pixels.
        raise FilingPDFError('pdf_unsupported_image_codec')
    if '/Decode' in image and not _numbers(_object(image['/Decode']), count * 2):
        raise FilingPDFError('pdf_invalid_image_decode')
    return count, bits



def _document_findings(root):
    findings = set()
    if '/AcroForm' in root:
        form = _object(root['/AcroForm'])
        if not isinstance(form, dict) or not isinstance(_object(form.get('/Fields')), list):
            raise FilingPDFError('pdf_invalid_interactive_form')
        if set(form) != {'/Fields'} or _object(form['/Fields']):
            raise FilingPDFError('pdf_unsupported_interactive_form_capability')
        findings.add('interactive_form')
    for key in ('/OCProperties', '/Names', '/AF', '/OpenAction', '/AA', '/Collection'):
        if key in root:
            # These graphs can alter visible content or embed further documents;
            # key presence is not a complete, valid independent visual receipt.
            raise FilingPDFError('pdf_unsupported_document_object_capability')
    return findings


def _annotation_inventory(page):
    from pypdf.generic import NameObject, TextStringObject, ByteStringObject, BooleanObject
    annotations = _object(page.get('/Annots', []))
    if not isinstance(annotations, list):
        raise FilingPDFError('pdf_invalid_annotation_inventory')
    for reference in annotations:
        obj = _object(reference)
        if not isinstance(obj, dict):
            raise FilingPDFError('pdf_invalid_annotation')
        if '/Type' in obj and (not isinstance(obj['/Type'], NameObject) or obj['/Type'] != '/Annot'):
            raise FilingPDFError('pdf_invalid_annotation')
        if not isinstance(obj.get('/Subtype'), NameObject) or obj['/Subtype'] != '/Text':
            raise FilingPDFError('pdf_unsupported_annotation_capability')
        rect = _object(obj.get('/Rect'))
        if not _numbers(rect, 4):
            raise FilingPDFError('pdf_invalid_annotation_geometry')
        for key, value in obj.items():
            value = _object(value)
            if key in ('/Type', '/Subtype', '/Rect'):
                continue
            if key in ('/Contents', '/NM', '/M', '/T', '/Subj', '/CreationDate'):
                if not isinstance(value, (TextStringObject, ByteStringObject)):
                    raise FilingPDFError('pdf_invalid_annotation')
            elif key == '/F':
                if not isinstance(value, int) or value < 0:
                    raise FilingPDFError('pdf_invalid_annotation')
            elif key == '/Open':
                if not isinstance(value, BooleanObject):
                    raise FilingPDFError('pdf_invalid_annotation')
            elif key == '/Name':
                if not isinstance(value, NameObject) or value not in {
                        '/Comment', '/Key', '/Note', '/Help', '/NewParagraph', '/Paragraph', '/Insert'}:
                    raise FilingPDFError('pdf_invalid_annotation')
            elif key == '/C':
                if not _numbers(value) or len(value) not in (0, 1, 3, 4):
                    raise FilingPDFError('pdf_invalid_annotation')
            else:
                # Includes AP appearance streams, widgets, popup/reply/action
                # graphs and optional-content memberships, none inventoried.
                raise FilingPDFError('pdf_unsupported_annotation_dependency')
    return len(annotations)


def _matrix(a, b):
    result = (a[0]*b[0]+a[1]*b[2], a[0]*b[1]+a[1]*b[3],
              a[2]*b[0]+a[3]*b[2], a[2]*b[1]+a[3]*b[3],
              a[4]*b[0]+a[5]*b[2]+b[4], a[4]*b[1]+a[5]*b[3]+b[5])
    if not all(math.isfinite(value) for value in result):
        raise FilingPDFError('pdf_invalid_derived_geometry')
    return result


def _operand_receipt(value):
    from pypdf.generic import TextStringObject, ByteStringObject, NameObject, BooleanObject
    value = _object(value)
    if isinstance(value, NameObject): return str(value)
    if isinstance(value, BooleanObject): return value.value
    if isinstance(value, (TextStringObject, ByteStringObject)):
        raw = _raw_text(value)
        return {'string_bytes':len(raw), 'string_sha256':_sha(raw)}
    if isinstance(value, (list, tuple)): return [_operand_receipt(v) for v in value]
    if isinstance(value, dict): return {str(k):_operand_receipt(v) for k,v in value.items()}
    if isinstance(value, (int,float)) and math.isfinite(value): return value
    if value is None: return None
    raise FilingPDFError('pdf_uninventoried_operand')


def _raw_text(value):
    if isinstance(value, bytes): return bytes(value)
    try: return value.original_bytes
    except Exception: raise FilingPDFError('pdf_text_original_bytes_unavailable') from None


def _inventory_append(state, target, value):
    size = len(json.dumps(value, sort_keys=True, separators=(',',':'), ensure_ascii=True, allow_nan=False).encode())
    budget = state['stream_budget']
    budget['inventory_bytes'] += size
    state['inventory_bytes'] = budget['inventory_bytes']
    if state['inventory_bytes'] > state['inventory_limit']:
        raise FilingPDFError('pdf_stream_limit_provenance')
    target.append(value)


def _validate_stream_dictionary(stream, kind):
    """Reject untraversed dependencies before asking a generic decoder for bytes."""
    from pypdf.generic import NameObject, NullObject
    if not hasattr(stream,'get_data') or not isinstance(stream,dict):
        raise FilingPDFError('pdf_invalid_stream_object')
    extra = {
        'embedded_font':{'/Length1'}, 'font_cmap':set(), 'font_cid_map':set(), 'font_cid_set':set(), 'page':set(),
        'form':{'/Type','/Subtype','/BBox','/Matrix','/Resources','/FormType'},
    }[kind]
    if not set(stream) <= {'/Length','/Filter','/DecodeParms'} | extra:
        raise FilingPDFError('pdf_unsupported_font_stream_dependency' if kind.startswith('font') or kind=='embedded_font'
                             else 'pdf_unsupported_content_stream_dependency')
    if kind=='form' and '/FormType' in stream and (type(stream['/FormType']) is bool or stream['/FormType'] != 1):
        raise FilingPDFError('pdf_invalid_form_type')
    filters = _object(stream.get('/Filter', []))
    filters = [filters] if isinstance(filters, NameObject) else filters
    lossless = {'/FlateDecode','/Fl','/ASCIIHexDecode','/AHx','/ASCII85Decode','/A85',
                '/LZWDecode','/LZW','/RunLengthDecode','/RL'}
    if not isinstance(filters, list) or any(not isinstance(f,NameObject) or f not in lossless for f in filters):
        raise FilingPDFError('pdf_unsupported_content_stream_filter')
    parameters = _object(stream.get('/DecodeParms'))
    if parameters is not None and not isinstance(parameters,NullObject):
        parameters = parameters if isinstance(parameters,list) else [parameters]
        if len(parameters) != len(filters): raise FilingPDFError('pdf_invalid_stream_parameters')
        for filter_name, param in zip(filters,parameters):
            param = _object(param)
            if isinstance(param,NullObject): continue
            keys = ({'/Predictor','/Colors','/BitsPerComponent','/Columns'} if filter_name in ('/FlateDecode','/Fl','/LZWDecode','/LZW') else set())
            if filter_name in ('/LZWDecode','/LZW'): keys = keys | {'/EarlyChange'}
            if not isinstance(param,dict) or not set(param)<=keys:
                raise FilingPDFError('pdf_unsupported_stream_parameters')
            if any(not isinstance(v,int) or isinstance(v,bool) for v in param.values()):
                raise FilingPDFError('pdf_invalid_stream_parameters')
            if param.get('/Predictor',1) != 1:
                raise FilingPDFError('pdf_unsupported_stream_predictor')
            if param.get('/EarlyChange',1) not in (0,1):
                raise FilingPDFError('pdf_invalid_stream_parameters')


def _strict_stream_data(stream, remaining, check, *, identity_parameters=True):
    """Decode only complete unfiltered/single-Flate streams, within the balance.

    Generic PDF filter recovery may return bytes despite truncated zlib framing
    or ignore an additional compressed stream. Neither is source-complete
    evidence. Other codecs/chains remain unsupported until their exact framing
    and parameters have an independently bounded implementation.
    """
    from pypdf.generic import NameObject, NullObject
    _pulse(check)
    if (not isinstance(stream,dict) or not hasattr(stream,'_data')
            or not isinstance(stream._data,bytes)):
        raise FilingPDFError('pdf_invalid_stream_object')
    if (remaining() if callable(remaining) else remaining) < 0: raise FilingPDFError('pdf_stream_limit')
    filters = _object(stream.get('/Filter', []))
    filters = [filters] if isinstance(filters,NameObject) else filters
    if (not isinstance(filters,list) or any(not isinstance(value,NameObject) for value in filters)
            or list(map(str,filters)) not in ([], ['/FlateDecode'], ['/Fl'])):
        raise FilingPDFError('pdf_unsupported_strict_stream_filter')
    parameters = _object(stream.get('/DecodeParms'))
    if parameters is not None and not isinstance(parameters,NullObject):
        parameters = parameters if isinstance(parameters,list) else [parameters]
        if len(parameters)!=len(filters): raise FilingPDFError('pdf_invalid_stream_parameters')
        for parameter in parameters:
            parameter = _object(parameter)
            if isinstance(parameter,NullObject): continue
            if (not identity_parameters or not isinstance(parameter,dict) or hasattr(parameter,'_data')
                    or not set(parameter)<={'/Predictor','/Colors','/BitsPerComponent','/Columns'}):
                raise FilingPDFError('pdf_unsupported_stream_parameters')
            if any(not isinstance(value,int) or isinstance(value,bool) for value in parameter.values()):
                raise FilingPDFError('pdf_invalid_stream_parameters')
            if parameter.get('/Predictor',1)!=1:
                raise FilingPDFError('pdf_unsupported_stream_predictor')
            if (parameter.get('/Colors',1)<=0 or parameter.get('/Columns',1)<=0
                    or parameter.get('/BitsPerComponent',8) not in (1,2,4,8,16)):
                raise FilingPDFError('pdf_invalid_stream_parameters')
    remaining = remaining() if callable(remaining) else remaining
    if remaining < 0: raise FilingPDFError('pdf_stream_limit')
    data = stream._data
    if filters:
        decoder = zlib.decompressobj()
        try: data = decoder.decompress(data,remaining+1)
        except zlib.error: raise FilingPDFError('pdf_invalid_flate_stream') from None
        if len(data)>remaining or decoder.unconsumed_tail:
            raise FilingPDFError('pdf_stream_limit')
        if not decoder.eof or decoder.unused_data:
            raise FilingPDFError('pdf_incomplete_or_trailing_flate_stream')
    if len(data)>remaining: raise FilingPDFError('pdf_stream_limit')
    _pulse(check)
    return data


def _consume_stream_data(stream, budget, limit, check, *, local_limit=None, identity_parameters=True):
    def remaining():
        available = limit-budget['decoded_stream_bytes']
        return min(available,local_limit) if local_limit is not None else available
    data = _strict_stream_data(stream,remaining,check,identity_parameters=identity_parameters)
    budget['decoded_stream_bytes'] += len(data)
    return data


def _object_stream_grammar(data, count, first, reader, check):
    """Consume every declared compressed object, including otherwise unused ones.

    pypdf resolves individual requested objects and can ignore malformed header
    suffixes or extra objects after the final declaration. Such recovery cannot
    establish a complete parser-stream inventory.
    """
    from pypdf.generic import read_object, StreamObject
    whitespace = rb'(?:[\x00\t\n\f\r ]|%[^\r\n]*(?:\r\n?|\n))*'
    final_whitespace = rb'(?:[\x00\t\n\f\r ]|%[^\r\n]*(?:\r\n?|\n|$))*'
    header = data[:first]
    position = 0
    pairs = []
    identities = set()
    for _ in range(count):
        _pulse(check)
        pair = []
        for _ in range(2):
            position = re.compile(whitespace).match(header, position).end()
            token = re.compile(rb'\+?[0-9]+(?=[\x00\t\n\f\r %]|$)').match(header, position)
            if token is None:
                raise FilingPDFError('pdf_invalid_object_stream_header')
            pair.append(int(token[0])); position = token.end()
        number, offset = pair
        if (number <= 0 or number in identities or offset >= len(data)-first
                or pairs and offset <= pairs[-1][1]):
            raise FilingPDFError('pdf_invalid_object_stream_header')
        identities.add(number); pairs.append((number, offset))
    if re.fullmatch(whitespace, header[position:]) is None:
        raise FilingPDFError('pdf_invalid_object_stream_header')
    prefix_end = first + pairs[0][1] if pairs else len(data)
    if re.fullmatch(whitespace, data[first:prefix_end]) is None:
        raise FilingPDFError('pdf_invalid_object_stream_content')
    for index, (_, offset) in enumerate(pairs):
        _pulse(check)
        stop = first+pairs[index+1][1] if index+1<len(pairs) else len(data)
        chunk = data[first+offset:stop]
        stream = io.BytesIO(chunk)
        stream.seek(re.match(whitespace, chunk).end())
        value = read_object(stream, reader)
        if isinstance(value, StreamObject) or re.fullmatch(
                final_whitespace if index+1==len(pairs) else whitespace, chunk[stream.tell():]) is None:
            raise FilingPDFError('pdf_invalid_object_stream_content')


def _strict_pdf_reader(decoded, budget, limit, check):
    """Reader-local validation before pypdf consumes XRef/ObjStm streams.

    The hook runs when an indirect stream is first cached, which precedes the
    library's parser-internal get_data calls. Only this reader's decoded cache
    is populated; original encoded bytes stay intact, with no global patch.
    """
    from pypdf import PdfReader
    from pypdf.generic import NameObject, NullObject, DecodedStreamObject, EncodedStreamObject

    class StrictReader(PdfReader):
        def __init__(self):
            self._strict_parser_streams = {}
            self.parser_stream_receipts = []
            self._strict_active_parser_streams = set()
            super().__init__(io.BytesIO(decoded),strict=True)

        def cache_indirect_object(self, generation, idnum, value):
            if isinstance(value,dict) and value.get('/Type') in ('/XRef','/ObjStm'):
                key = (generation,idnum)
                if key in self._strict_active_parser_streams:
                    raise _Cancelled(FilingPDFError('pdf_parser_stream_cycle'))
                previous = self._strict_parser_streams.get(key)
                if previous is not None and previous is not value:
                    raise _Cancelled(FilingPDFError('pdf_duplicate_parser_stream'))
                if previous is None:
                    self._strict_active_parser_streams.add(key)
                    try:
                        data,receipt = self._strict_parser_data(value)
                        receipt.update(index=len(self.parser_stream_receipts),object_number=idnum,generation=generation)
                        size = len(json.dumps(receipt,sort_keys=True,separators=(',',':')).encode())
                        budget['inventory_bytes'] = budget.get('inventory_bytes',0)+size
                        if budget['inventory_bytes']>limit: raise FilingPDFError('pdf_stream_limit_provenance')
                        self.parser_stream_receipts.append(receipt)
                        decoded_stream = DecodedStreamObject()
                        decoded_stream.set_data(data)
                        if isinstance(value,EncodedStreamObject): value.decoded_self = decoded_stream
                        elif not isinstance(value,DecodedStreamObject):
                            raise FilingPDFError('pdf_invalid_parser_stream')
                        self._strict_parser_streams[key] = value
                    except FilingPDFError as error:
                        # pypdf can wrap/recover ValueError while reading XRef;
                        # preserve the strict rejection through that boundary.
                        raise _Cancelled(error) from None
                    finally:
                        self._strict_active_parser_streams.remove(key)
            return super().cache_indirect_object(generation,idnum,value)

        def _strict_parser_data(self, value):
            _pulse(check)
            kind = value.get('/Type')
            common = {'/Type','/Length','/Filter','/DecodeParms'}
            extra = ({'/Size','/W','/Index','/Prev','/Root','/Info','/ID','/Encrypt'}
                     if kind=='/XRef' else {'/N','/First'})
            if not isinstance(kind,NameObject) or not set(value)<=common|extra:
                raise FilingPDFError('pdf_unsupported_parser_stream_dependency')
            def natural(number):
                return isinstance(number,int) and not isinstance(number,bool) and number>=0
            columns = entries = None
            if kind=='/XRef':
                size = value.get('/Size');widths = _object(value.get('/W'))
                indices = _object(value.get('/Index',[0,size]))
                if (not natural(size) or size==0 or not isinstance(widths,list) or len(widths)!=3
                        or any(not natural(width) or width>8 for width in widths) or not sum(widths)
                        or not isinstance(indices,list) or not indices or len(indices)%2
                        or any(not natural(index) for index in indices)):
                    raise FilingPDFError('pdf_invalid_xref_stream')
                end = 0; entries = 0; columns = sum(widths)
                for offset in range(0,len(indices),2):
                    start,count = indices[offset:offset+2]
                    if start<end or start+count>size: raise FilingPDFError('pdf_invalid_xref_stream')
                    end = start+count; entries += count
                if entries*columns>limit-budget['decoded_stream_bytes']:
                    raise FilingPDFError('pdf_stream_limit')
                if '/Prev' in value and (not natural(value['/Prev']) or not 0<value['/Prev']<len(decoded)):
                    raise FilingPDFError('pdf_invalid_xref_stream')
            else:
                if not natural(value.get('/N')) or not natural(value.get('/First')):
                    raise FilingPDFError('pdf_invalid_object_stream')
            parameters = _object(value.get('/DecodeParms'))
            if isinstance(parameters,list) and len(parameters)==1: parameters = _object(parameters[0])
            predictor = parameters.get('/Predictor',1) if isinstance(parameters,dict) else 1
            if kind=='/XRef' and predictor==12:
                # The retained XRef producer uses PNG Up with one 8-bit channel.
                # Validate that exact transform, then inflate unchanged encoded
                # bytes through the common strict framing/budget implementation.
                if (not isinstance(parameters,dict) or hasattr(parameters,'_data')
                        or not set(parameters)<={'/Predictor','/Columns','/Colors','/BitsPerComponent'}
                        or any(not natural(v) for v in parameters.values())
                        or parameters.get('/Colors',1)!=1 or parameters.get('/BitsPerComponent',8)!=8
                        or parameters.get('/Columns',1)!=columns):
                    raise FilingPDFError('pdf_unsupported_xref_predictor')
                filter_value = _object(value.get('/Filter'))
                if isinstance(filter_value,list) and len(filter_value)==1: filter_value = filter_value[0]
                if not isinstance(filter_value,NameObject) or filter_value not in ('/FlateDecode','/Fl'):
                    raise FilingPDFError('pdf_unsupported_xref_predictor')
                compressed = EncodedStreamObject(); compressed._data = value._data
                compressed[NameObject('/Filter')] = filter_value
                filtered = _consume_stream_data(compressed,budget,limit,check)
                if len(filtered)!=entries*(columns+1): raise FilingPDFError('pdf_invalid_xref_predictor_rows')
                data = bytearray(); previous = bytes(columns)
                for offset in range(0,len(filtered),columns+1):
                    _pulse(check)
                    if filtered[offset]!=2: raise FilingPDFError('pdf_invalid_xref_predictor_row')
                    previous = bytes((a+b)&255 for a,b in zip(filtered[offset+1:offset+columns+1],previous))
                    data.extend(previous)
                # Charge inflated bytes, including predictor row bytes; the
                # reconstructed data is no larger and is not a second decode.
                data = bytes(data)
                inflated = filtered
            else:
                data = _consume_stream_data(value,budget,limit,check)
                inflated = data
            if kind=='/XRef' and len(data)!=entries*columns:
                raise FilingPDFError('pdf_invalid_xref_stream_length')
            if kind=='/ObjStm' and (value['/First']>len(data) or value['/N']>value['/First']//3):
                raise FilingPDFError('pdf_invalid_object_stream')
            if kind=='/ObjStm':
                try:
                    _object_stream_grammar(data,value['/N'],value['/First'],self,check)
                except FilingPDFError:
                    raise
                except Exception:
                    raise FilingPDFError('pdf_invalid_object_stream_content') from None
            def parameters_declaration(item):
                item = _object(item)
                if item is None or isinstance(item,NullObject): return None
                if isinstance(item,list): return [parameters_declaration(v) for v in item]
                if isinstance(item,dict): return {str(k):int(v) for k,v in item.items()}
                raise FilingPDFError('pdf_invalid_stream_parameters')
            filters = _object(value.get('/Filter',[]))
            filters = [filters] if isinstance(filters,NameObject) else filters
            budget['parser_stream_bytes'] = budget.get('parser_stream_bytes',0)+len(inflated)
            receipt = dict(kind='xref' if kind=='/XRef' else 'object',
                encoded_sha256=_sha(value._data),encoded_bytes=len(value._data),
                inflated_sha256=_sha(inflated),inflated_bytes=len(inflated),
                decoded_sha256=_sha(data),decoded_bytes=len(data),charged_bytes=len(inflated),
                filters=list(map(str,filters)),decode_parameters=parameters_declaration(value.get('/DecodeParms')),
                predictor=int(predictor),columns=int(columns) if columns is not None else None,
                rows=int(entries) if entries is not None else None,
                object_count=int(value['/N']) if kind=='/ObjStm' else None,
                first_object_byte=int(value['/First']) if kind=='/ObjStm' else None)
            return data,receipt

    return StrictReader()


def validate_pdf_stream_accounting(receipt):
    """Shape/accounting checks only; original bytes must be re-extracted in audit."""
    def require(condition):
        if not condition: raise FilingPDFError('pdf_invalid_parser_stream_receipt')
    def natural(value): return type(value) is int and value>=0
    def digest(value): return isinstance(value,str) and re.fullmatch(r'[0-9a-f]{64}',value) is not None
    try:
        rows = receipt['parser_streams']; application = receipt['application_stream_bytes']
        require(isinstance(rows,list) and natural(application) and natural(receipt['decoded_stream_bytes']))
        keys = {'index','object_number','generation','kind','encoded_sha256','encoded_bytes','inflated_sha256',
                'inflated_bytes','decoded_sha256','decoded_bytes','charged_bytes','filters','decode_parameters',
                'predictor','columns','rows','object_count','first_object_byte'}
        charged = 0; identities = set()
        for index,row in enumerate(rows):
            require(isinstance(row,dict) and set(row)==keys)
            require(type(row['index']) is int and row['index']==index)
            require(natural(row['object_number']) and row['object_number']>0 and natural(row['generation']))
            identity = (row['generation'],row['object_number'])
            require(identity not in identities); identities.add(identity)
            for key in ('encoded_bytes','inflated_bytes','decoded_bytes','charged_bytes'): require(natural(row[key]))
            for key in ('encoded_sha256','inflated_sha256','decoded_sha256'): require(digest(row[key]))
            require(row['kind'] in ('xref','object') and row['filters'] in ([],['/FlateDecode'],['/Fl']))
            require(type(row['predictor']) is int and row['predictor'] in (1,12))
            params = row['decode_parameters']
            if isinstance(params,list):
                require(len(params)==len(row['filters'])); params = params[0] if params else None
            if params is not None:
                require(bool(row['filters']) and isinstance(params,dict)
                        and set(params)<={'/Predictor','/Colors','/BitsPerComponent','/Columns'}
                        and all(type(v) is int for v in params.values()))
                require(params.get('/Predictor',1)==row['predictor'] and params.get('/Colors',1)>0
                        and params.get('/Columns',1)>0 and params.get('/BitsPerComponent',8) in (1,2,4,8,16))
            else: require(row['predictor']==1)
            require(row['charged_bytes']==row['inflated_bytes'])
            if row['predictor']==1:
                require(row['inflated_bytes']==row['decoded_bytes'] and row['inflated_sha256']==row['decoded_sha256'])
            else:
                require(row['kind']=='xref' and bool(row['filters']) and isinstance(params,dict)
                        and params.get('/Colors',1)==1 and params.get('/BitsPerComponent',8)==8
                        and params.get('/Columns',1)==row['columns'])
            if not row['filters']:
                require(row['encoded_bytes']==row['inflated_bytes'] and row['encoded_sha256']==row['inflated_sha256'])
            if row['kind']=='xref':
                require(natural(row['columns']) and 0<row['columns']<=24 and natural(row['rows']))
                require(row['decoded_bytes']==row['columns']*row['rows'])
                require(row['object_count'] is None and row['first_object_byte'] is None)
                if row['predictor']==12: require(row['inflated_bytes']==(row['columns']+1)*row['rows'])
            else:
                require(row['predictor']==1 and row['columns'] is None and row['rows'] is None)
                require(natural(row['object_count']) and natural(row['first_object_byte'])
                        and row['first_object_byte']<=row['decoded_bytes'] and row['object_count']<=row['first_object_byte']//3)
            charged += row['charged_bytes']
        require(receipt['decoded_stream_bytes']==application+charged)
        if receipt.get('native_text_complete') is True:
            amounts = [stream['bytes'] for page in receipt['pages'] for stream in page['streams']]
            require(all(natural(value) for value in amounts) and sum(amounts)==application)
        return True
    except FilingPDFError: raise
    except (KeyError,TypeError,ValueError,OverflowError,RecursionError):
        raise FilingPDFError('pdf_invalid_parser_stream_receipt') from None


def _font_decoder(font, state, check, max_stream_bytes):
    from .filing_pdf_fonts import build_font, FontError
    cache = state['font_cache']
    key = id(font)
    if key not in cache:
        def consume(stream, kind):
            _validate_stream_dictionary(stream, kind)
            declared = stream.get('/Length1')
            if declared is not None and (not isinstance(declared,int) or isinstance(declared,bool) or declared <= 0):
                raise FilingPDFError('pdf_invalid_font_stream_length')
            if declared is not None and declared > max_stream_bytes-state['stream_budget']['decoded_stream_bytes']:
                raise FilingPDFError('pdf_stream_limit')
            _pulse(check)
            data = _consume_stream_data(stream,state['stream_budget'],max_stream_bytes,check,
                                        local_limit=8192 if kind=='font_cid_set' else None,
                                        identity_parameters=kind!='font_cid_set')
            if declared is not None and declared != len(data):
                raise FilingPDFError('pdf_invalid_font_stream_length')
            if not data: raise FilingPDFError('pdf_empty_font_stream')
            state['streams'].append({'kind':kind,'sha256':_sha(data),'bytes':len(data)})
            return data
        try: cache[key] = build_font(font, consume_stream=consume, check=lambda:_pulse(check))
        except FontError as error: raise FilingPDFError(str(error)) from None
    decoder = cache[key]
    receipt = decoder.receipt
    identity = _sha(json.dumps(receipt, sort_keys=True, separators=(',',':')).encode())
    if identity not in state['fonts']:
        state['fonts'][identity] = receipt
        _inventory_append(state, state['font_inventory'], {'font_id':identity, **receipt})
    return identity, decoder


def _text_state():
    return dict(cm=(1.,0.,0.,1.,0.,0.), tm=(1.,0.,0.,1.,0.,0.), line=(1.,0.,0.,1.,0.,0.),
                font=None, font_id=None, font_size=0., char_space=0., word_space=0., scale=1., leading=0., rise=0., render=0,
                fill_color=(0.,), fill_alpha=1.)


def _move_line(g, x, y):
    g['line'] = _matrix((1.,0.,0.,1.,x,y), g['line'])
    g['tm'] = g['line']


def _show(state, g, strings, operator, stream_hash, index, check):
    from .filing_pdf_fonts import FontError
    parts=[]; decoded_parts=[]; start=g['tm']; glyph_count=0
    for value in strings:
        _pulse(check)
        if isinstance(value,(int,float)):
            if not math.isfinite(value): raise FilingPDFError('pdf_invalid_text_operands')
            advance=-float(value)/1000*g['font_size']*g['scale']
            g['tm']=_matrix((1.,0.,0.,1.,advance,0.),g['tm'])
            parts.append({'adjustment':float(value),'advance':advance})
            continue
        raw=_raw_text(value)
        try: decoded=g['font'].decode(raw, state['inventory_limit']-state['stream_budget']['inventory_bytes'])
        except FontError as error: raise FilingPDFError(str(error)) from None
        text=decoded['text']; decoded_parts.append(text)
        glyph_count+=len(decoded['glyphs'])
        advance=sum((glyph['width']/1000*g['font_size']+g['char_space']+
                     (g['word_space'] if g['font'].code_bytes==1 and glyph['code']==32 else 0))*g['scale']
                    for glyph in decoded['glyphs'])
        parts.append({'raw_sha256':_sha(raw),'raw_bytes':len(raw),'unicode':text,'glyphs':decoded['glyphs'],
                      'text_matrix':list(g['tm']),'advance':advance})
        g['tm']=_matrix((1.,0.,0.,1.,advance,0.),g['tm'])
    text=''.join(decoded_parts)
    if not all(math.isfinite(v) for v in (*g['tm'],*g['cm'],g['char_space'],g['word_space'],g['scale'],g['rise'])):
        raise FilingPDFError('pdf_invalid_text_geometry')
    row={'stream_sha256':stream_hash,'operator_index':index,'operator':operator.decode(),
         'font_id':g['font_id'],'font_size':g['font_size'],'ctm':list(g['cm']),
         'text_matrix':list(start),'end_text_matrix':list(g['tm']),
         'char_space':g['char_space'],'word_space':g['word_space'],'horizontal_scale':g['scale'],
         'rise':g['rise'],'render_mode':g['render'],'parts':parts,'unicode':text,'glyph_count':glyph_count}
    _inventory_append(state,state['text_shows'],row)
    # Content invocation order is authoritative; the only inserted separator is
    # a recorded line break when source-derived baseline changes.
    baseline=_matrix(start,g['cm'])[5]
    separator='\n' if state['text_chunks'] and text and abs(baseline-state['last_baseline'])>.01 else ''
    row['separator_before']=separator
    state['text_chars']+=len(text)+len(separator)
    if state['text_chars']>state['max_text_chars']:raise FilingPDFError('pdf_text_limit')
    state['text_chunks'].append(separator+text)
    state['last_baseline']=baseline
    state['plain_runs'].append((text,tuple(g['cm']),tuple(start),g['font_size']))
    if text and g['render'] != 0:
        state['findings'].add('nondefault_text_rendering')
    if text and (g['char_space'] or g['word_space'] or g['scale'] != 1 or g['rise'] or any(p.get('adjustment',0) for p in parts)):
        state['findings'].add('positioned_text_layout')
    black = ((0.,0.,0.,1.) if len(g['fill_color']) == 4 else (0.,)*len(g['fill_color']))
    if text and (g['fill_alpha'] != 1 or g['fill_color'] != black):
        state['support_limitations'].add('text_visibility_state_not_validated')


def _content_operations(data, check):
    """Read exact operator boundaries; reject operands stranded at EOF.

    pypdf's general ContentStream parser drops final operands at EOF. This
    bounded native subset uses its object lexer but requires full consumption.
    Inline image decoding is deliberately outside this finite subset.
    """
    from pypdf.generic import read_object, NameObject
    from pypdf._utils import read_non_whitespace, read_until_regex
    stream = io.BytesIO(data)
    operands = []
    start = 0
    while True:
        _pulse(check)
        token = read_non_whitespace(stream)
        if not token:
            if operands: raise FilingPDFError('pdf_trailing_content_operands')
            return
        if token == b'%':
            while token not in (b'\r',b'\n',b''):
                token = stream.read(1)
            continue
        stream.seek(-1,1)
        if not operands: start = stream.tell()
        if token.isalpha() or token in (b"'",b'"'):
            operator = read_until_regex(stream=stream, regex=NameObject.delimiter_pattern, length=64)
            if operator == b'BI': raise FilingPDFError('pdf_unsupported_inline_image_capability')
            yield operands, operator, start, stream.tell()
            operands = []
        else:
            operands.append(read_object(stream, None))


def _inspect_stream(stream, resources, reader, state, check, max_stream_bytes, active=(), graphics=(1, 1), inherited=None):
    from pypdf.generic import TextStringObject, ByteStringObject, NameObject
    _pulse(check)
    resolved = _object(stream)
    references = resolved if isinstance(resolved,list) else ([] if resolved is None else [resolved])
    blocks = []
    for reference in references:
        source = _object(reference)
        _validate_stream_dictionary(source,'form' if active else 'page')
        block = _consume_stream_data(source,state['stream_budget'],max_stream_bytes,check)
        blocks.append(block)
    # A content array is the exact concatenation of its source streams. Adding
    # a newline can repair invalid tokens or alter a split literal string.
    data = b''.join(blocks)
    digest = _sha(data)
    state['streams'].append({'kind': 'form' if active else 'page', 'sha256': digest, 'bytes': len(data)})
    if not isinstance(resources, dict):
        raise FilingPDFError('pdf_invalid_resource_dictionary')
    # Device defaults apply even when painting uses the implicit initial gray.
    # Refuse unvalidated remapping before it can become visual-deferral proof.
    for space in _DEVICE_COLORS:
        _color_components(NameObject(space), resources)
    depth, saved, marked, compatibility, font = 0, 0, 0, 0, None
    colors, graphics_stack = list(graphics), []
    g = dict(inherited) if inherited else _text_state()
    text_stack = []
    for operator_index, (operands, operator, byte_start, byte_end) in enumerate(_content_operations(data, check)):
        _pulse(check)
        if operator not in _KNOWN:
            raise FilingPDFError('pdf_unknown_content_operator')
        if operator in _ZERO_OPERANDS and operands:
            raise FilingPDFError('pdf_invalid_content_operands')
        if operator in _NUMERIC_OPERANDS and (len(operands) != _NUMERIC_OPERANDS[operator]
                or any(not isinstance(value, (int, float)) or not math.isfinite(value) for value in operands)):
            raise FilingPDFError('pdf_invalid_content_operands')
        _visual_operator(operator, operands, resources, colors)
        state['operators'].add(operator)
        _inventory_append(state, state['geometry_operations'], {'stream_sha256':digest, 'operator_index':operator_index,
            'operator':operator.decode(), 'operands':_operand_receipt(operands), 'ctm':list(g['cm']),
            'source_byte_start':byte_start, 'source_byte_end':byte_end})
        if operator == b'BX':
            compatibility += 1
        elif operator == b'EX':
            compatibility -= 1
            if compatibility < 0: raise FilingPDFError('pdf_unbalanced_compatibility')
        elif operator == b'gs':
            ext = _resource(resources, '/ExtGState', operands[0])
            if '/ca' in ext: g['fill_alpha'] = float(ext['/ca'])
            if any(key in ext for key in ('/OP','/op','/OPM','/SA','/AIS','/TK')):
                state['support_limitations'].add('graphics_semantics_not_validated')
            _inventory_append(state, state['graphics_resources'], {'category':'ExtGState','name':str(operands[0]),
                'parameters':_operand_receipt(ext)})
        elif operator == b'BT':
            if depth:
                raise FilingPDFError('pdf_unbalanced_text')
            depth = 1
            g['tm'] = g['line'] = (1.,0.,0.,1.,0.,0.)
        elif operator == b'ET':
            if not depth:
                raise FilingPDFError('pdf_unbalanced_text')
            depth = 0
        elif operator == b'q':
            saved += 1
            graphics_stack.append(colors[:])
            text_stack.append(dict(g))
        elif operator == b'Q':
            saved -= 1
            if saved < 0:
                raise FilingPDFError('pdf_unbalanced_graphics')
            colors[:] = graphics_stack.pop()
            prior = text_stack.pop()
            prior['tm'], prior['line'] = g['tm'], g['line']
            g = prior
        elif operator in (b'BMC', b'BDC'):
            marked += 1
            state['findings'].add('marked_content')
        elif operator == b'EMC':
            marked -= 1
            if marked < 0:
                raise FilingPDFError('pdf_unbalanced_marked_content')
        elif operator == b'Tf':
            if (len(operands) != 2 or not isinstance(operands[0], NameObject)
                    or not isinstance(operands[1], (int, float))
                    or not math.isfinite(operands[1]) or operands[1] <= 0):
                raise FilingPDFError('pdf_invalid_font')
            fonts = _object(resources.get('/Font', {}))
            font = _object(fonts.get(operands[0]))
            if not isinstance(font, dict):
                raise FilingPDFError('pdf_missing_font')
            g['font_id'], g['font'] = _font_decoder(font, state, check, max_stream_bytes)
            g['font_size'] = float(operands[1])
            if g['font'].standard and any(key in font for key in ('/Widths','/FirstChar','/LastChar')):
                state['support_limitations'].add('font_geometry_not_validated')
            if not g['font'].standard:
                state['advanced_font'] = True
        elif operator in _TEXT:
            if not depth or g['font'] is None:
                raise FilingPDFError('pdf_text_without_font_or_object')
            arity = 3 if operator == b'\"' else 1
            if len(operands) != arity:
                raise FilingPDFError('pdf_invalid_text_operands')
            string_types = (TextStringObject, ByteStringObject)
            if operator == b'TJ':
                if not isinstance(operands[0], list) or any(
                        not isinstance(value, (*string_types, int, float)) for value in operands[0]):
                    raise FilingPDFError('pdf_invalid_text_operands')
                strings = operands[0]
            else:
                if not isinstance(operands[-1], string_types) or (arity == 3 and any(
                        not isinstance(value, (int, float)) for value in operands[:2])):
                    raise FilingPDFError('pdf_invalid_text_operands')
                strings = [operands[-1]]
            if operator in (b"'", b'"'):
                if operator == b'"':
                    g['word_space'], g['char_space'] = map(float, operands[:2])
                _move_line(g, 0., -g['leading'])
            _show(state, g, strings, operator, digest, operator_index, check)
            state['text_shows'][-1].update(source_byte_start=byte_start,source_byte_end=byte_end)
            state['native_text_operations'] += 1
            if any(isinstance(value, string_types) and len(value) for value in strings):
                state['nonempty_text_operations'] += 1
        elif operator == b'cs':
            g['fill_color'] = (0.,0.,0.,1.) if colors[1] == 4 else (0.,)*colors[1]
        elif operator in (b'g',b'rg',b'k',b'sc',b'scn'):
            g['fill_color'] = tuple(map(float, operands))
        elif operator == b'cm':
            g['cm'] = _matrix(tuple(map(float,operands)), g['cm'])
        elif operator == b'Tm':
            g['tm'] = g['line'] = tuple(map(float,operands))
        elif operator in (b'Td', b'TD'):
            if operator == b'TD': g['leading'] = -float(operands[1])
            _move_line(g, *map(float,operands))
        elif operator == b'T*':
            _move_line(g, 0., -g['leading'])
        elif operator in (b'Tc',b'Tw',b'Tz',b'TL',b'Ts'):
            key = {b'Tc':'char_space',b'Tw':'word_space',b'Tz':'scale',b'TL':'leading',b'Ts':'rise'}[operator]
            g[key] = float(operands[0]) / (100 if operator == b'Tz' else 1)
        elif operator == b'Tr':
            if len(operands) != 1 or not isinstance(operands[0], int) or not 0 <= operands[0] <= 7:
                raise FilingPDFError('pdf_invalid_text_render_mode')
            g['render'] = int(operands[0])

        elif operator in _PAINT:
            state['vector_paint_operations'] += 1
            state['findings'].add('vector_graphics')
        elif operator in (b'W', b'W*'):
            state['findings'].add('clipping_path')
        elif operator == b'INLINE IMAGE':
            raise FilingPDFError('pdf_unsupported_inline_image_capability')
        elif operator == b'Do':
            if len(operands) != 1:
                raise FilingPDFError('pdf_invalid_xobject')
            obj = _object(_object(resources.get('/XObject', {})).get(operands[0]))
            if not isinstance(obj, dict):
                raise FilingPDFError('pdf_missing_xobject')
            if obj.get('/Subtype') == '/Image':
                if any(not isinstance(obj.get(key), int) or obj[key] <= 0 for key in ('/Width', '/Height')):
                    raise FilingPDFError('pdf_invalid_image')
                components, bits = _image_components(obj, resources)
                _pulse(check)
                image_data = _consume_stream_data(obj,state['stream_budget'],max_stream_bytes,check)
                expected_image_bytes = ((obj['/Width'] * components * bits + 7) // 8) * obj['/Height']
                if len(image_data) != expected_image_bytes:
                    raise FilingPDFError('pdf_invalid_decoded_image_length')
                if not image_data:
                    raise FilingPDFError('pdf_empty_image_stream')
                state['streams'].append({'kind': 'image', 'sha256': _sha(image_data), 'bytes': len(image_data)})
                state['image_operations'] += 1
                state['findings'].add('raster_image')
            elif obj.get('/Subtype') == '/Form':
                if id(obj) in active or len(active) >= 100:
                    raise FilingPDFError('pdf_cyclic_form')
                if (not _numbers(_object(obj.get('/BBox')), 4)
                        or ('/Matrix' in obj and not _numbers(_object(obj['/Matrix']), 6))):
                    raise FilingPDFError('pdf_invalid_form_geometry')
                if any(key in obj for key in ('/Group', '/Ref', '/OPI', '/OC')):
                    raise FilingPDFError('pdf_unsupported_form_dependency')
                state['form_operations'] += 1
                state['findings'].add('form_xobject')
                _inspect_stream(obj, _object(obj.get('/Resources', resources)), reader, state,
                                check, max_stream_bytes, (*active, id(obj)), tuple(colors),
                                {**g, 'cm':_matrix(tuple(map(float,obj.get('/Matrix',(1,0,0,1,0,0)))),g['cm'])})
            else:
                raise FilingPDFError('pdf_unsupported_xobject')
    if depth or saved or marked or compatibility:
        raise FilingPDFError('pdf_unbalanced_content')
    return data


def _plain_layout(page, state, runs):
    """Recognize only plain standard-font lines; never infer table absence.

    Every show is one literal Tj, with an identity user transform, upright text
    matrix, equal left edges, descending baselines and conservative one-em glyph
    bounds within an unrotated, uncropped page. V2 fully inventories the finite
    advanced native subset separately, without asserting its visual layout is
    plain. Any actual residual capability limit remains a technical failure.
    """
    if not runs:
        return state['findings'] == {'blank_page'} and not state['operators']
    box = [float(value) for value in page.mediabox]
    if '/CropBox' in page and [float(value) for value in page['/CropBox']] != box:
        state['findings'].add('cropped_page')
        return False
    previous, left, size = None, None, None
    for text, cm, tm, font_size in runs:
        if cm != (1., 0., 0., 1., 0., 0.) or tm[:4] != (1., 0., 0., 1.):
            state['findings'].add('transformed_text')
            return False
        x, y = tm[4:]
        if (re.search(r'\s{2,}', text) or (left is not None and abs(x - left) > .01)
                or (previous is not None and previous - y < 1.2 * font_size)):
            state['findings'].add('positioned_text_layout')
            return False
        if not state['advanced_font'] and size is not None and size != font_size:
            state['support_limitations'].add('mixed_font_geometry_not_validated')
            return False
        if not (box[0] <= x and x + len(text) * font_size <= box[2]
                and box[1] + font_size <= y <= box[3] - font_size):
            state['findings'].add('text_outside_conservative_page_bounds')
        previous, left, size = y, x, font_size
    return (not state['advanced_font'] and not (state['findings'] - {'marked_content'})
            and not state['support_limitations'])


def extract_pdf_evidence(body: bytes, *, max_pdf_bytes: int, max_text_chars: int,
                         max_pages: int, max_stream_bytes: int, check=None) -> dict:
    """Extract every page or raise a technical error; never return partial evidence.

    ``max_pdf_bytes`` bounds both the supplied body and decoded PDF. Stream bytes
    bound each decoder and aggregate document page/form/image content (repeated forms count
    again). Text chars include inter-page newlines. ``check`` exceptions propagate
    unchanged, including from visitors; outer process deadlines remain required.
    Source submission/document identities and original submission hash belong to
    the framing caller and must be retained alongside this body-bound envelope.
    """
    if any(type(value) is not int or value <= 0 for value in
           (max_pdf_bytes, max_text_chars, max_pages, max_stream_bytes)):
        raise FilingPDFError('pdf_invalid_limits')
    try:
        decoded, encoding = _decode(body, max_pdf_bytes, check)
        try:
            import pypdf
            from pypdf import PdfReader, apply_configuration
            from pypdf.errors import LimitReachedError
        except ImportError:
            raise FilingPDFError('pdf_dependency_unavailable') from None
        caps = dict(maximum_declared_stream_length=max_stream_bytes,
            array_based_stream_maximum_output_length=max_stream_bytes,
            zlib_maximum_output_length=max_stream_bytes, zlib_maximum_recovery_input_length=0,
            lzw_maximum_output_length=max_stream_bytes, run_length_maximum_output_length=max_stream_bytes,
            brotli_maximum_output_length=max_stream_bytes, jbig2_maximum_output_length=max_stream_bytes,
            stream_decoding_work_maximum_length=max_stream_bytes,
            image_maximum_buffer_size=max_stream_bytes, flate_maximum_row_length=max_stream_bytes,
            page_tree_maximum_entries=max_pages * 4 + 100, page_tree_maximum_depth=100,
            xform_maximum_invocations_per_extraction=min(max_stream_bytes, 5000), jbig2dec_binary=None)
        with apply_configuration(**caps), _diagnostics() as notices:
            try:
                _pulse(check)
                stream_budget = {'decoded_stream_bytes':0,'inventory_bytes':0}
                reader = _strict_pdf_reader(decoded,stream_budget,max_stream_bytes,check)
                if reader.is_encrypted:
                    raise FilingPDFError('pdf_encrypted')
                count = _page_tree(reader, max_pages, check)
                if len(reader.pages) != count:
                    raise FilingPDFError('pdf_page_count_mismatch')
                document_findings = _document_findings(reader.root_object)
                pages, chars, stream_bytes = [], 0, 0
                font_cache = {}
                inventory_bytes = stream_budget['inventory_bytes']
                for index, page in enumerate(reader.pages):
                    _pulse(check)
                    state = dict(stream_budget=stream_budget, streams=[], operators=set(), findings=set(document_findings),
                        support_limitations=set(), native_text_operations=0, nonempty_text_operations=0,
                        image_operations=0, vector_paint_operations=0, form_operations=0,
                        font_cache=font_cache, fonts={}, font_inventory=[], text_shows=[], geometry_operations=[], graphics_resources=[],
                        text_chunks=[], text_chars=chars, max_text_chars=max_text_chars, last_baseline=0.,
                        inventory_bytes=inventory_bytes, inventory_limit=max_stream_bytes, advanced_font=False, plain_runs=[])
                    if page.rotation % 360:
                        state['findings'].add('rotated_page')
                    annotation_count = _annotation_inventory(page)
                    if annotation_count:
                        state['findings'].add('annotations')
                    if any(key in page for key in ('/AF', '/AA', '/PresSteps', '/VP')):
                        raise FilingPDFError('pdf_unsupported_page_object_capability')
                    resources = _object(page.get('/Resources', {}))
                    if '/Group' in page:
                        _page_group(page['/Group'], resources)
                    data = _inspect_stream(page.get('/Contents'), resources, reader, state, check, max_stream_bytes)
                    stream_bytes = stream_budget['decoded_stream_bytes']
                    text = ''.join(state['text_chunks'])
                    inventory_bytes = stream_budget['inventory_bytes']
                    runs = state['plain_runs']
                    if any((ord(char) < 32 and char not in '\n\r\t') or char == '\ufffd'
                           or 0xD800 <= ord(char) <= 0xDFFF for char in text):
                        raise FilingPDFError('pdf_text_encoding_loss')
                    if state['nonempty_text_operations'] and not text.strip():
                        raise FilingPDFError('pdf_missing_native_text')
                    if not text.strip():
                        if state['image_operations']:
                            state['findings'].add('image_only_page')
                        elif state['vector_paint_operations']:
                            state['findings'].add('vector_only_page')
                        elif state['operators'] or annotation_count:
                            raise FilingPDFError('pdf_unexplained_empty_page')
                        else:
                            state['findings'].add('blank_page')
                    supported = _plain_layout(page, state, runs)
                    chars += len(text) + (1 if pages else 0)
                    if chars > max_text_chars:
                        raise FilingPDFError('pdf_text_limit')
                    if notices:
                        raise FilingPDFError('pdf_parser_recovery_warning')
                    pages.append(dict(page_number=index + 1, text=text, text_sha256=_sha(text.encode('utf-8')),
                        text_chars=len(text), content_sha256=_sha(data), content_bytes=len(data),
                        streams=state['streams'], fonts=state['font_inventory'], text_shows=state['text_shows'],
                        geometry_operations=state['geometry_operations'], graphics_resources=state['graphics_resources'],
                        page_geometry={'media_box':list(map(float,page.mediabox)),
                            'crop_box':list(map(float,page.cropbox)), 'rotation':page.rotation,
                            'group':_operand_receipt(page.get('/Group'))}, mapping_trust='source_declared_unicode', findings=sorted(state['findings']),
                        support_limitations=sorted(state['support_limitations']), supported_text_layout=supported,
                        observations={key: state[key] for key in ('native_text_operations', 'image_operations',
                            'vector_paint_operations', 'form_operations')}, annotation_count=annotation_count))
                text = '\n'.join(page['text'] for page in pages)
                _pulse(check)
                if notices:
                    raise FilingPDFError('pdf_parser_recovery_warning')
                receipt = dict(version=VERSION, schema_version=3, input_sha256=_sha(body), input_bytes=len(body),
                    decoded_sha256=_sha(decoded), decoded_bytes=len(decoded), encoding=encoding,
                    library='pypdf', library_version=pypdf.__version__, page_count=count, pages=pages,
                    decoded_stream_bytes=stream_budget['decoded_stream_bytes'],
                    parser_streams=reader.parser_stream_receipts,
                    application_stream_bytes=stream_budget['decoded_stream_bytes']-stream_budget.get('parser_stream_bytes',0),
                    text=text, text_sha256=_sha(text.encode('utf-8')), text_chars=len(text),
                    findings=sorted({item for page in pages for item in page['findings']}),
                    support_limitations=sorted({item for page in pages for item in page['support_limitations']}),
                    native_text_complete=True, mapping_trust='source_declared_unicode',
                    text_order='content_invocation_order_with_recorded_baseline_separators', supported_text_layout=all(page['supported_text_layout'] for page in pages),
                    visual_meaning_verified=False)
                validate_pdf_stream_accounting(receipt)
                return receipt
            except LimitReachedError:
                raise FilingPDFError('pdf_stream_or_structure_limit') from None
            except FilingPDFError:
                raise
            except Exception:
                raise FilingPDFError('pdf_malformed_or_extraction_failed') from None
    except _Cancelled as cancelled:
        raise cancelled.error from None
