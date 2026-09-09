"""Text repair: the part that decides whether narration sounds like a book."""

from __future__ import annotations

import pytest

from pdf_audiobook.extract import (
    CITATION_RE,
    PAGE_NUMBER_RE,
    Book,
    Chapter,
    _fingerprint,
    _looks_like_running_head,
    _normalise,
    clean_page,
    find_running_heads,
    load_pdf,
)


class TestNormalise:
    def test_expands_ligatures(self):
        assert _normalise("the ﬁrst ﬂame") == "the first flame"

    def test_straightens_quotes(self):
        assert _normalise("“hello,” he said") == '"hello," he said'

    def test_em_dash_becomes_a_spoken_pause(self):
        # Read verbatim, a bare em dash is silence; " - " is a pause.
        assert " - " in _normalise("wait—no")

    def test_strips_unprintables_but_keeps_newlines(self):
        assert _normalise("a\x00b\nc") == "ab\nc"


class TestRunningHeadDetection:
    def test_fingerprint_collapses_digits(self):
        assert _fingerprint("Page 12") == _fingerprint("Page 87")

    def test_long_lines_are_never_headers(self):
        assert not _looks_like_running_head("x" * 81)

    def test_a_sentence_is_not_a_header(self):
        # The bug the eval harness caught: body text at a page edge was being
        # deleted as furniture. A long line ending in a full stop is prose.
        line = "Sailors moved between the nets with unhurried care."
        assert len(line) > 40
        assert not _looks_like_running_head(line)

    def test_a_short_label_is_a_header(self):
        assert _looks_like_running_head("A Test Book")

    def test_needs_to_recur_across_pages(self):
        pages = [["A Test Book"], ["A Test Book"], ["A Test Book"], ["one off"]]
        heads = find_running_heads(pages)
        assert _fingerprint("A Test Book") in heads
        assert _fingerprint("one off") not in heads

    def test_below_threshold_is_kept(self):
        # Threshold is max(3, 25% of pages) - twice is not a running head.
        pages = [["A Test Book"], ["A Test Book"], ["prose"], ["prose"]]
        assert _fingerprint("A Test Book") not in find_running_heads(pages)


class TestPatterns:
    @pytest.mark.parametrize("line", ["12", " 47 ", "- 12 -", "| 47", "iv 12"])
    def test_page_numbers_match(self, line):
        assert PAGE_NUMBER_RE.match(line)

    @pytest.mark.parametrize("line", ["Chapter 12 begins", "1984 was a year"])
    def test_prose_is_not_a_page_number(self, line):
        assert not PAGE_NUMBER_RE.match(line)

    @pytest.mark.parametrize("cite", ["[12]", "[1,3]", "[Smith 2004]"])
    def test_citations_match(self, cite):
        assert CITATION_RE.fullmatch(cite)


class TestCleanPage:
    def test_removes_furniture_and_rejoins_hyphens(self):
        heads = {_fingerprint("A Test Book")}
        raw = (
            "A Test Book\n"
            "The harbour was quiet that morning and the boats had\n"
            "not yet gone out, and under-\n"
            "standing passed between them.\n"
            "12\n"
        )
        out = clean_page(raw, heads)

        assert "A Test Book" not in out
        assert "understanding" in out       # de-hyphenated across the break
        assert "under-" not in out
        assert not out.strip().endswith("12")

    def test_strips_citations_and_urls(self):
        out = clean_page("The claim is well established [12] see www.example.com\n", set())
        assert "[12]" not in out
        assert "example.com" not in out

    def test_drops_lines_with_no_letters(self):
        out = clean_page("....\n----\nReal prose here.\n", set())
        assert "Real prose here." in out
        assert "----" not in out

    def test_reflows_hard_wrapped_lines_into_one_paragraph(self):
        raw = "The harbour was quiet\nand the boats had not\nyet gone out today.\n"
        out = clean_page(raw, set())
        # Hard wraps become spaces, so the TTS engine does not pause mid-sentence.
        assert "\n" not in out.strip()


class TestLoadPdf:
    def test_uses_the_embedded_outline(self, novel_pdf):
        book = load_pdf(novel_pdf)
        assert book.used_toc
        assert [c.title for c in book.chapters] == ["Chapter 1", "Chapter 2", "Chapter 3"]

    def test_strips_furniture_end_to_end(self, novel_pdf):
        book = load_pdf(novel_pdf)
        text = "\n".join(c.text for c in book.chapters)
        assert "A Test Book" not in text
        assert "Prakhar Srivastava" not in text
        assert "understanding" in text

    def test_falls_back_to_page_chunks_without_an_outline(self, plain_pdf):
        book = load_pdf(plain_pdf, pages_per_part=2)
        assert not book.used_toc
        assert all(c.title.startswith("Part ") for c in book.chapters)

    def test_page_range_is_one_based_and_inclusive(self, novel_pdf):
        whole = load_pdf(novel_pdf)
        part = load_pdf(novel_pdf, first_page=3, last_page=4)
        assert part.word_count < whole.word_count

    def test_empty_range_is_rejected(self, novel_pdf):
        with pytest.raises(ValueError, match="Empty page range"):
            load_pdf(novel_pdf, first_page=5, last_page=2)

    def test_scanned_pdf_gets_an_actionable_error(self, tmp_path):
        try:
            import pymupdf as fitz
        except ImportError:  # pragma: no cover
            import fitz
        blank = tmp_path / "scan.pdf"
        doc = fitz.open()
        doc.new_page()
        doc.save(blank)
        doc.close()

        with pytest.raises(ValueError, match="OCR"):
            load_pdf(blank)


class TestModels:
    def test_word_and_time_estimates_roll_up(self):
        book = Book(title="t", author="a", page_count=2, chapters=[
            Chapter(1, "One", 0, 1, text="one two three"),
            Chapter(2, "Two", 1, 2, text="four five"),
        ])
        assert book.word_count == 5
        assert book.est_minutes == pytest.approx(5 / 180)
