"""Pull clean, speakable text out of a PDF.

Raw PDF text is a mess for TTS: lines are hard-wrapped mid-sentence, words are
split with hyphens across line breaks, and every page repeats a header, a
footer and a page number. Read aloud verbatim, that sounds broken. This module
undoes all of it.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field

try:  # PyMuPDF >= 1.24 prefers the `pymupdf` name; `fitz` is the old alias.
    import pymupdf as fitz
except ImportError:  # pragma: no cover - older PyMuPDF
    import fitz

# Measured from the neural voices at their default speed. Only an estimate;
# a --rate of +25% shortens everything by about a fifth.
WORDS_PER_MINUTE = 180.0


@dataclass
class Chapter:
    """One output audio file's worth of text."""

    index: int
    title: str
    start_page: int  # 0-based, inclusive
    end_page: int    # 0-based, exclusive
    text: str = ""

    @property
    def word_count(self) -> int:
        return len(self.text.split())

    @property
    def est_minutes(self) -> float:
        """Rough listening time, measured against the neural voices at +0%."""
        return self.word_count / WORDS_PER_MINUTE


@dataclass
class Book:
    title: str
    author: str
    page_count: int
    chapters: list[Chapter] = field(default_factory=list)
    used_toc: bool = False

    @property
    def word_count(self) -> int:
        return sum(c.word_count for c in self.chapters)

    @property
    def est_minutes(self) -> float:
        return sum(c.est_minutes for c in self.chapters)


# --------------------------------------------------------------------------
# text cleanup
# --------------------------------------------------------------------------

LIGATURES = {
    "ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi",
    "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st",
}

PUNCTUATION = {
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "–": "-", "—": " - ", "…": "...", " ": " ",
}

# A line that is nothing but a page number, or "12 | Chapter Three".
PAGE_NUMBER_RE = re.compile(r"^\s*[ivxlcdm]*\s*[-–—|]?\s*\d{1,4}\s*[-–—|]?\s*$", re.I)
# Inline citation / footnote markers: [12], [1,3], [Smith 2004]
CITATION_RE = re.compile(r"\[\s*(?:\d+(?:\s*[,–-]\s*\d+)*|[A-Z][A-Za-z]+\s+\d{4})\s*\]")
URL_RE = re.compile(r"\b(?:https?://|www\.)\S+")
# Hyphen split across a line break: "under-\nstanding" -> "understanding"
HYPHEN_BREAK_RE = re.compile(r"(\w)[-‐‑]\n(\w)")
SENTENCE_END_RE = re.compile(r"[.!?:;\"')\]]$")


def _normalise(text: str) -> str:
    for src, dst in LIGATURES.items():
        text = text.replace(src, dst)
    for src, dst in PUNCTUATION.items():
        text = text.replace(src, dst)
    text = unicodedata.normalize("NFKC", text)
    # Strip anything unprintable that would confuse the TTS tokeniser.
    return "".join(ch for ch in text if ch in "\n\t" or ch.isprintable())


def _fingerprint(line: str) -> str:
    """Collapse a line to a shape, so 'Page 12' and 'Page 87' match."""
    return re.sub(r"\d+", "#", line.strip().lower())


def _looks_like_running_head(line: str) -> bool:
    """Headers and footers are short labels, not prose.

    Without this guard a body sentence that lands at the top or bottom of the
    page often enough would be deleted from the narration.
    """
    if len(line) > 80:
        return False
    # A long-ish line ending in a full stop is a sentence, not a header.
    return not (line.endswith(".") and len(line) > 40)


# Fraction of the page height, top and bottom, treated as the margin. A
# running head lives here; body text does not.
MARGIN_BAND = 0.10


def margin_lines(page, band: float = MARGIN_BAND) -> list[str]:
    """Text physically sitting in the top or bottom margin of the page.

    Position is what actually distinguishes a running head from prose. Using
    "the first couple of lines of the text stream" instead would delete real
    sentences that happen to land at a page edge on several pages.
    """
    rect = page.rect
    top = rect.y0 + rect.height * band
    bottom = rect.y1 - rect.height * band

    found: list[str] = []
    for block in page.get_text("blocks"):
        y0, y1, text = block[1], block[3], block[4]
        if not isinstance(text, str):
            continue  # an image block
        if y1 <= top or y0 >= bottom:
            found.extend(ln.strip() for ln in text.split("\n") if ln.strip())
    return found


def find_running_heads(pages: list[list[str]]) -> set[str]:
    """Header/footer lines that repeat across the margins of most pages.

    Three signals have to agree before text is discarded: it sits in a margin,
    it looks like a label rather than prose, and it recurs.
    """
    counts: Counter[str] = Counter()
    for lines in pages:
        for line in set(lines):
            if _looks_like_running_head(line):
                counts[_fingerprint(line)] += 1

    threshold = max(3, int(len(pages) * 0.25))
    return {shape for shape, n in counts.items() if n >= threshold}


def _reflow(lines: list[str]) -> str:
    """Join hard-wrapped lines back into real paragraphs."""
    if not lines:
        return ""

    lengths = sorted(len(ln) for ln in lines)
    median = lengths[len(lengths) // 2] or 1

    paragraphs: list[str] = []
    buf: list[str] = []

    for line in lines:
        buf.append(line)
        short = len(line) < median * 0.55
        # A short line that also closes a sentence is almost always a real
        # paragraph end rather than an accident of the page width.
        if short and SENTENCE_END_RE.search(line):
            paragraphs.append(" ".join(buf))
            buf = []

    if buf:
        paragraphs.append(" ".join(buf))

    return "\n\n".join(paragraphs)


def clean_page(text: str, running_heads: set[str], drop_urls: bool = True) -> str:
    text = _normalise(text)
    text = HYPHEN_BREAK_RE.sub(r"\1\2", text)

    kept: list[str] = []
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            continue
        if _fingerprint(line) in running_heads:
            continue
        if PAGE_NUMBER_RE.match(line):
            continue
        # A line of dot leaders, rules or stray symbols has nothing to say.
        if not re.search(r"[^\W\d_]", line):
            continue
        kept.append(line)

    text = _reflow(kept)
    text = CITATION_RE.sub("", text)
    if drop_urls:
        text = URL_RE.sub("", text)

    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" ([,.;:!?])", r"\1", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# --------------------------------------------------------------------------
# chapter layout
# --------------------------------------------------------------------------

def _chapters_from_toc(doc: fitz.Document, max_level: int = 1) -> list[Chapter]:
    """Use the PDF's own outline as the chapter list, when it has one."""
    toc = doc.get_toc(simple=True)
    if not toc:
        return []

    entries = [(title, page - 1) for level, title, page in toc
               if level <= max_level and page > 0]
    if len(entries) < 2:
        return []

    chapters: list[Chapter] = []
    for i, (title, start) in enumerate(entries):
        end = entries[i + 1][1] if i + 1 < len(entries) else doc.page_count
        if end <= start:  # several outline entries landing on the same page
            continue
        chapters.append(Chapter(index=len(chapters) + 1, title=title.strip(),
                                start_page=start, end_page=end))

    # Anything before the first outline entry is front matter.
    if chapters and chapters[0].start_page > 0:
        chapters.insert(0, Chapter(0, "Front Matter", 0, chapters[0].start_page))
        for i, ch in enumerate(chapters):
            ch.index = i

    return chapters


# Above this, a chapter is long enough to be worth splitting further.
TARGET_PAGES_PER_CHAPTER = 40


def _pick_toc_chapters(doc: fitz.Document, level: str | int = "auto") -> list[Chapter]:
    """Choose how deep to read the PDF outline.

    Top-level entries are the right split for an ordinary novel, but in an
    omnibus or a boxed set level 1 is the *books*, which would each become a
    single twenty-hour file. When the top level gives huge parts and a deeper
    level exists, go deeper.
    """
    if level != "auto":
        return _chapters_from_toc(doc, int(level))

    chosen: list[Chapter] = []
    for depth in (1, 2, 3, 4):
        chapters = _chapters_from_toc(doc, depth)
        if not chapters:
            continue
        chosen = chapters
        spans = sorted(c.end_page - c.start_page for c in chapters)
        if spans[len(spans) // 2] <= TARGET_PAGES_PER_CHAPTER:
            break
    return chosen


def _chapters_by_pages(pages_per_part: int, first: int, last: int) -> list[Chapter]:
    chapters: list[Chapter] = []
    for start in range(first, last, pages_per_part):
        end = min(start + pages_per_part, last)
        idx = len(chapters) + 1
        chapters.append(Chapter(index=idx,
                                title=f"Part {idx:02d} (pages {start + 1}-{end})",
                                start_page=start, end_page=end))
    return chapters


def load_pdf(
    path: str,
    first_page: int = 1,
    last_page: int | None = None,
    pages_per_part: int = 25,
    use_toc: bool = True,
    toc_level: str | int = "auto",
    clean: bool = True,
    password: str | None = None,
) -> Book:
    """Read `path` and return a `Book` with per-chapter speakable text.

    `first_page` / `last_page` are 1-based and inclusive, matching what a
    reader sees in a PDF viewer.
    """
    doc = fitz.open(path)
    try:
        if doc.needs_pass and not doc.authenticate(password or ""):
            raise ValueError("This PDF is password protected. Pass --password.")

        first = max(0, first_page - 1)
        last = doc.page_count if last_page is None else min(last_page, doc.page_count)
        if first >= last:
            raise ValueError(
                f"Empty page range {first_page}-{last_page}; the PDF has {doc.page_count} pages."
            )

        pages = [doc.load_page(p) for p in range(doc.page_count)]
        raw_pages = [page.get_text("text") for page in pages]
        running_heads = (find_running_heads([margin_lines(p) for p in pages[first:last]])
                         if clean else set())

        meta = doc.metadata or {}
        book = Book(title=(meta.get("title") or "").strip(),
                    author=(meta.get("author") or "").strip(),
                    page_count=doc.page_count)

        chapters: list[Chapter] = []
        if use_toc:
            chapters = [c for c in _pick_toc_chapters(doc, toc_level)
                        if c.end_page > first and c.start_page < last]
            if chapters:
                # Clip the outline to the requested page range.
                chapters[0].start_page = max(chapters[0].start_page, first)
                chapters[-1].end_page = min(chapters[-1].end_page, last)
                book.used_toc = True

        if not chapters:
            chapters = _chapters_by_pages(pages_per_part, first, last)

        for ch in chapters:
            parts = []
            for p in range(ch.start_page, ch.end_page):
                parts.append(clean_page(raw_pages[p], running_heads) if clean
                             else _normalise(raw_pages[p]))
            ch.text = "\n\n".join(t for t in parts if t).strip()

        book.chapters = [c for c in chapters if c.word_count >= 5]
    finally:
        doc.close()

    if not book.chapters:
        raise ValueError(
            "No readable text found. This PDF is most likely a scan of page images - "
            "run it through OCR first, e.g. `ocrmypdf in.pdf out.pdf`."
        )
    return book
