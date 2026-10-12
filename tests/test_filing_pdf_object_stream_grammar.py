"""Independent regressions: decoded ObjStm data must be wholly consumed."""
import pytest

from test_filing_pdf_parser_streams import parser_stream_pdf, native_extract, visual_extract


def altered_object_stream(change):
    return parser_stream_pdf(objects=True, object_mutation=change)[0]


@pytest.mark.parametrize('extractor', [native_extract, visual_extract])
@pytest.mark.parametrize('change', [
    lambda raw:raw + b' ignored malformed content',
    lambda raw:raw + b' 123',
    lambda raw:raw + b' << /Hidden /Object >>',
    lambda raw:raw.replace(b'5 0 ', b'5 0x'),
], ids=['trailing_garbage','extra_number','extra_dictionary','malformed_header'])
def test_object_stream_must_contain_exactly_declared_objects(extractor, change):
    with pytest.raises(ValueError):
        extractor(altered_object_stream(change))


@pytest.mark.parametrize('extractor', [native_extract, visual_extract])
@pytest.mark.parametrize('suffix', [b' \t\n', b' % final comment', b'\n% comment\r\n'])
def test_object_stream_trailing_pdf_whitespace_and_comments_are_valid(extractor, suffix):
    assert extractor(altered_object_stream(lambda raw:raw+suffix))['page_count'] == 1
