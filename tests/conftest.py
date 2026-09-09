"""Shared fixtures.

`eval.py` measures end-to-end extraction accuracy against generated books.
These tests are the other half: unit-level checks on the pieces, so a
regression points at a function rather than at a percentage.
"""

from __future__ import annotations

import wave
from pathlib import Path

import pytest

try:
    import pymupdf as fitz
except ImportError:  # pragma: no cover - older PyMuPDF
    import fitz


BODY = (
    "The harbour was quiet that morning and the boats had not yet gone out. "
    "Sailors moved between the nets with the unhurried care of people who have "
    "done a thing ten thousand times. Nobody spoke above a murmur."
)


def _draw(page, lines, top=90, size=11, leading=16):
    """Write lines onto a page at fixed leading, imitating typeset prose."""

    y = top
    for line in lines:
        page.insert_text((72, y), line, fontsize=size)
        y += leading


@pytest.fixture
def novel_pdf(tmp_path: Path) -> Path:
    """A three-chapter book with a real table of contents and page furniture.

    Every page carries a running header, a footer and a page number, so
    anything that reads this PDF has to strip them to get clean text.
    """

    path = tmp_path / "novel.pdf"
    doc = fitz.open()
    toc = []

    for chapter in range(1, 4):
        for page_no in range(2):
            page = doc.new_page()
            number = len(doc)
            # Running header and footer, in the page margins.
            page.insert_text((72, 40), "A Test Book", fontsize=9)
            page.insert_text((72, 780), "Prakhar Srivastava", fontsize=9)
            page.insert_text((300, 800), str(number), fontsize=9)

            lines = []
            if page_no == 0:
                lines.append(f"Chapter {chapter}")
                toc.append([1, f"Chapter {chapter}", number])
            # Hard-wrapped prose, plus a word hyphenated across a line break.
            lines += [
                "The harbour was quiet that morning and the boats had",
                "not yet gone out. Sailors moved between the nets with",
                "the unhurried care of people who have done a thing ten",
                "thousand times. Nobody spoke above a murmur, and under-",
                "standing passed between them without a word.",
            ]
            _draw(page, lines)

    doc.set_toc(toc)
    doc.save(path)
    doc.close()
    return path


@pytest.fixture
def plain_pdf(tmp_path: Path) -> Path:
    """Four pages, no outline at all — the page-chunking path."""

    path = tmp_path / "plain.pdf"
    doc = fitz.open()
    for _ in range(4):
        page = doc.new_page()
        _draw(page, [BODY[i:i + 60] for i in range(0, len(BODY), 60)])
    doc.save(path)
    doc.close()
    return path


@pytest.fixture
def wav_parts(tmp_path: Path) -> list[Path]:
    """Three tiny WAV files sharing a format, for the merge tests."""

    parts = []
    for i in range(3):
        path = tmp_path / f"part{i}.wav"
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(22050)
            handle.writeframes(b"\x00\x01" * 1000)
        parts.append(path)
    return parts
