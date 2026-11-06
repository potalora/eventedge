"""Independent adversarial cases for the limited structural PDF contract."""
import pytest
from pypdf.generic import (
    ArrayObject, DecodedStreamObject, DictionaryObject, NameObject,
    NumberObject, TextStringObject,
)

from test_filing_pdf import _rewrite_pdf
from test_filing_pdf_visual import image_pdf, extract


@pytest.mark.parametrize('value', [TextStringObject('broken'), NumberObject(-1)])
def test_invalid_page_user_unit_is_not_structural_success(value):
    def change(writer):
        writer.pages[0][NameObject('/UserUnit')] = value
    with pytest.raises(ValueError):
        extract(_rewrite_pdf(image_pdf(), change))


def test_unknown_page_transition_is_not_structural_success():
    def change(writer):
        writer.pages[0][NameObject('/Trans')] = DictionaryObject({
            NameObject('/S'): NameObject('/Unrecognized'),
        })
    with pytest.raises(ValueError):
        extract(_rewrite_pdf(image_pdf(), change))


def test_invalid_output_profile_dependency_is_not_structural_success():
    def change(writer):
        writer.root_object[NameObject('/OutputIntents')] = ArrayObject([
            DictionaryObject({NameObject('/DestOutputProfile'): TextStringObject('broken')}),
        ])
    with pytest.raises(ValueError):
        extract(_rewrite_pdf(image_pdf(), change))


def test_page_transparency_group_is_preserved_in_structural_receipt():
    def change(writer):
        writer.pages[0][NameObject('/Group')] = DictionaryObject({
            NameObject('/S'): NameObject('/Transparency'),
            NameObject('/CS'): NameObject('/DeviceRGB'),
        })
    receipt = extract(_rewrite_pdf(image_pdf(), change))
    assert receipt['pages'][0]['page_geometry']['group'] == {
        '/S': '/Transparency', '/CS': '/DeviceRGB',
    }


def test_rotated_image_bounding_box_overlap_is_not_polygon_intersection():
    def change(writer):
        stream = DecodedStreamObject()
        # All four vertices satisfy x+y <= -0.5, so no point is on this
        # page (whose x,y are >= 0), despite its bounding-box overlap.
        stream.set_data(b'1.5 1.5 -1.5 1.5 -1 -2.5 cm /Im1 Do')
        writer.pages[0][NameObject('/Contents')] = writer._add_object(stream)
    receipt = extract(_rewrite_pdf(image_pdf(), change))
    invocation = receipt['pages'][0]['image_invocations'][0]
    assert invocation['positive_area_page_intersection'] is False
    assert receipt['visual_presence'] is False


@pytest.mark.parametrize('damage', ['image_type', 'image_subtype', 'font_subtype'])
def test_malformed_resource_discriminators_are_not_structural_success(damage):
    def change(writer):
        resources = writer.pages[0]['/Resources']
        image = resources['/XObject']['/Im1'].get_object()
        if damage == 'image_type':
            image[NameObject('/Type')] = NameObject('/Font')
        elif damage == 'image_subtype':
            image[NameObject('/Subtype')] = TextStringObject('/Image')
        else:
            resources['/Font']['/F1'].get_object()[NameObject('/Subtype')] = TextStringObject('/Type1')
    with pytest.raises(ValueError):
        extract(_rewrite_pdf(image_pdf(), change))


def test_shared_resource_dag_hits_existing_inventory_bound():
    def change(writer):
        node = writer._add_object(DictionaryObject())
        for _ in range(22):
            node = writer._add_object(DictionaryObject({
                NameObject('/A'): node, NameObject('/B'): node,
            }))
        writer.pages[0]['/Resources'][NameObject('/Properties')] = DictionaryObject({
            NameObject('/Shared'): node,
        })
    with pytest.raises(ValueError, match='inventory_limit'):
        extract(_rewrite_pdf(image_pdf(), change), max_stream_bytes=20000)


def test_cyclic_resource_declaration_is_rejected():
    def change(writer):
        node = DictionaryObject()
        reference = writer._add_object(node)
        node[NameObject('/Self')] = reference
        writer.pages[0]['/Resources'][NameObject('/Properties')] = DictionaryObject({
            NameObject('/Cycle'): reference,
        })
    with pytest.raises(ValueError, match='cycle_or_depth'):
        extract(_rewrite_pdf(image_pdf(), change))


@pytest.mark.parametrize('key', ['/MediaBox', '/CropBox'])
def test_page_box_strings_are_not_silently_coerced_to_numbers(key):
    def change(writer):
        writer.pages[0][NameObject(key)] = ArrayObject([
            TextStringObject(value) for value in ('0', '0', '612', '792')
        ])
    with pytest.raises(ValueError):
        extract(_rewrite_pdf(image_pdf(), change))
