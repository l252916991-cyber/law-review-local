import pytest

from app.rag_chunks import page_chunks


def test_chunks_preserve_source_offsets_and_cover_long_page():
    text = "  第一段证据。\n" * 120 + "末尾关键记录"
    chunks = page_chunks(text)
    assert len(chunks) > 2
    covered = set()
    for child in chunks:
        start, end = child["char_start"], child["char_end"]
        assert child["text"] == text[start:end]
        assert 0 < end - start <= 400
        covered.update(range(start, end))
    assert covered == set(range(len(text)))
    assert chunks[-1]["char_end"] == len(text)


def test_empty_and_invalid_chunk_parameters():
    assert page_chunks("") == []
    assert page_chunks(" \n ") == []
    with pytest.raises(ValueError):
        page_chunks("材料", size=10, overlap=10)
