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
import logging
import math
import re

VERSION = 'sec-native-pdf-evidence-v1'
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
        match = re.fullmatch(rb'[ \t\r\n]*<PDF>\r?\n(.*?)</PDF>[ \t\r\n]*', body, re.S)
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


def _properties(value):
    if not isinstance(value, dict):
        raise FilingPDFError('pdf_invalid_marked_properties')
    for key, reference in value.items():
        item = _object(reference)
        if key != '/MCID':
            raise FilingPDFError('pdf_unsupported_marked_properties')
        if not isinstance(item, int) or item < 0:
            raise FilingPDFError('pdf_invalid_marked_properties')


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
        _properties(value)
    elif operator in (b'd0', b'd1'):
        raise FilingPDFError('pdf_unsupported_type3_glyph_capability')


def _image_components(image, resources):
    from pypdf.generic import NameObject, BooleanObject
    if any(key in image for key in ('/Mask', '/SMask', '/Alternates', '/OPI', '/F', '/OC', '/Matte')):
        raise FilingPDFError('pdf_unsupported_image_dependency')
    if '/ImageMask' in image:
        mask = _object(image['/ImageMask'])
        if not isinstance(mask, BooleanObject) or mask.value:
            raise FilingPDFError('pdf_unsupported_or_invalid_image_mask')
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


def _inspect_stream(stream, resources, reader, state, check, max_stream_bytes, active=(), graphics=(1, 1)):
    from pypdf.generic import ContentStream, TextStringObject, ByteStringObject, NameObject
    _pulse(check)
    content = ContentStream(stream, reader)
    data = content.get_data()
    state['stream_bytes'] += len(data)
    if state['stream_bytes'] > max_stream_bytes:
        raise FilingPDFError('pdf_stream_limit')
    digest = _sha(data)
    state['streams'].append({'kind': 'form' if active else 'page', 'sha256': digest, 'bytes': len(data)})
    if not isinstance(resources, dict):
        raise FilingPDFError('pdf_invalid_resource_dictionary')
    # Device defaults apply even when painting uses the implicit initial gray.
    # Refuse unvalidated remapping before it can become visual-deferral proof.
    for space in _DEVICE_COLORS:
        _color_components(NameObject(space), resources)
    depth, saved, marked, font = 0, 0, 0, None
    colors, graphics_stack = list(graphics), []
    for operands, operator in content.operations:
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
        if operator == b'BT':
            if depth:
                raise FilingPDFError('pdf_unbalanced_text')
            depth = 1
        elif operator == b'ET':
            if not depth:
                raise FilingPDFError('pdf_unbalanced_text')
            depth = 0
        elif operator == b'q':
            saved += 1
            graphics_stack.append(colors[:])
        elif operator == b'Q':
            saved -= 1
            if saved < 0:
                raise FilingPDFError('pdf_unbalanced_graphics')
            colors[:] = graphics_stack.pop()
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
            encoding = _object(font.get('/Encoding'))
            standard = (font.get('/Subtype') == '/Type1' and font.get('/BaseFont') in _STANDARD
                        and '/FontDescriptor' not in font and encoding in (None, '/StandardEncoding', '/WinAnsiEncoding'))
            if not standard and '/ToUnicode' not in font:
                raise FilingPDFError('pdf_unverified_font_encoding')
            if not standard or any(key in font for key in ('/Widths', '/FirstChar', '/LastChar')):
                state['support_limitations'].add('font_geometry_not_validated')
        elif operator in _TEXT:
            if not depth or font is None:
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
            state['native_text_operations'] += 1
            if any(isinstance(value, string_types) and len(value) for value in strings):
                state['nonempty_text_operations'] += 1
        elif operator == b'Tr':
            if len(operands) != 1 or not isinstance(operands[0], int) or not 0 <= operands[0] <= 7:
                raise FilingPDFError('pdf_invalid_text_render_mode')
            if operands[0] != 0:
                state['findings'].add('nondefault_text_rendering')
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
                image_data = obj.get_data()
                expected_image_bytes = ((obj['/Width'] * components * bits + 7) // 8) * obj['/Height']
                if len(image_data) != expected_image_bytes:
                    raise FilingPDFError('pdf_invalid_decoded_image_length')
                state['stream_bytes'] += len(image_data)
                if not image_data:
                    raise FilingPDFError('pdf_empty_image_stream')
                if state['stream_bytes'] > max_stream_bytes:
                    raise FilingPDFError('pdf_stream_limit')
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
                                check, max_stream_bytes, (*active, id(obj)), tuple(colors))
            else:
                raise FilingPDFError('pdf_unsupported_xobject')
    if depth or saved or marked:
        raise FilingPDFError('pdf_unbalanced_content')
    return data


def _plain_layout(page, state, runs):
    """Recognize only plain standard-font lines; never infer table absence.

    Every show is one literal Tj, with an identity user transform, upright text
    matrix, equal left edges, descending baselines and conservative one-em glyph
    bounds within an unrotated, uncropped page. Custom fonts and other syntax
    have a capability limit, which is not independent visual-deferral evidence.
    """
    if state['native_text_operations'] and not state['operators'] <= _PLAIN:
        state['support_limitations'].add('plain_text_operator_subset_exceeded')
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
        if size is not None and size != font_size:
            state['support_limitations'].add('mixed_font_geometry_not_validated')
            return False
        if not (box[0] <= x and x + len(text) * font_size <= box[2]
                and box[1] + font_size <= y <= box[3] - font_size):
            state['findings'].add('text_outside_conservative_page_bounds')
        previous, left, size = y, x, font_size
    return not state['findings'] and not state['support_limitations']


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
                reader = PdfReader(io.BytesIO(decoded), strict=True)
                if reader.is_encrypted:
                    raise FilingPDFError('pdf_encrypted')
                count = _page_tree(reader, max_pages, check)
                if len(reader.pages) != count:
                    raise FilingPDFError('pdf_page_count_mismatch')
                document_findings = _document_findings(reader.root_object)
                pages, chars, stream_bytes = [], 0, 0
                for index, page in enumerate(reader.pages):
                    _pulse(check)
                    state = dict(stream_bytes=stream_bytes, streams=[], operators=set(), findings=set(document_findings),
                        support_limitations=set(), native_text_operations=0, nonempty_text_operations=0,
                        image_operations=0, vector_paint_operations=0, form_operations=0)
                    if page.rotation % 360:
                        state['findings'].add('rotated_page')
                    annotation_count = _annotation_inventory(page)
                    if annotation_count:
                        state['findings'].add('annotations')
                    if any(key in page for key in ('/Group', '/AF', '/AA', '/PresSteps', '/VP')):
                        raise FilingPDFError('pdf_unsupported_page_object_capability')
                    resources = _object(page.get('/Resources', {}))
                    data = _inspect_stream(page.get('/Contents'), resources, reader, state, check, max_stream_bytes)
                    stream_bytes = state['stream_bytes']
                    runs, visited_chars, current_size = [], 0, 0.
                    def before(operator, operands, cm, tm):
                        nonlocal current_size
                        _pulse(check)
                        if operator == b'Tf':
                            current_size = float(operands[1])
                        if operator in _TEXT:
                            matrices = tuple(float(value) for value in (*cm, *tm))
                            if not all(math.isfinite(value) for value in matrices):
                                raise FilingPDFError('pdf_invalid_text_geometry')
                            # Operand provenance; text is read again below only for layout checks.
                            if operator == b'Tj' and operands and isinstance(operands[0], (str, bytes)):
                                literal = operands[0].decode('latin-1') if isinstance(operands[0], bytes) else str(operands[0])
                                runs.append((literal, tuple(map(float, cm)), tuple(map(float, tm)), current_size))
                    def visitor(text, cm, tm, font, font_size):
                        nonlocal visited_chars
                        _pulse(check)
                        if text:
                            if font is None:
                                raise FilingPDFError('pdf_missing_font')
                            visited_chars += len(text)
                            if chars + visited_chars > max_text_chars:
                                raise FilingPDFError('pdf_text_limit')
                    text = page.extract_text(orientations=(0, 90, 180, 270),
                        visitor_operand_before=before, visitor_text=visitor)
                    _pulse(check)
                    if not isinstance(text, str):
                        raise FilingPDFError('pdf_text_extraction_failed')
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
                        streams=state['streams'], findings=sorted(state['findings']),
                        support_limitations=sorted(state['support_limitations']), supported_text_layout=supported,
                        observations={key: state[key] for key in ('native_text_operations', 'image_operations',
                            'vector_paint_operations', 'form_operations')}, annotation_count=annotation_count))
                text = '\n'.join(page['text'] for page in pages)
                _pulse(check)
                if notices:
                    raise FilingPDFError('pdf_parser_recovery_warning')
                return dict(version=VERSION, schema_version=1, input_sha256=_sha(body), input_bytes=len(body),
                    decoded_sha256=_sha(decoded), decoded_bytes=len(decoded), encoding=encoding,
                    library='pypdf', library_version=pypdf.__version__, page_count=count, pages=pages,
                    decoded_stream_bytes=stream_bytes,
                    text=text, text_sha256=_sha(text.encode('utf-8')), text_chars=len(text),
                    findings=sorted({item for page in pages for item in page['findings']}),
                    support_limitations=sorted({item for page in pages for item in page['support_limitations']}),
                    native_text_complete=True, supported_text_layout=all(page['supported_text_layout'] for page in pages),
                    visual_meaning_verified=False)
            except LimitReachedError:
                raise FilingPDFError('pdf_stream_or_structure_limit') from None
            except FilingPDFError:
                raise
            except Exception:
                raise FilingPDFError('pdf_malformed_or_extraction_failed') from None
    except _Cancelled as cancelled:
        raise cancelled.error from None
