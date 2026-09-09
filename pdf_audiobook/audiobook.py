"""Tie extraction and speech together: PDF in, folder of audio out."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .extract import Book, Chapter, load_pdf
from .tts import DEFAULT_CONCURRENCY, concat_files, get_engine

# Called as on_event(kind, payload). Kinds: "book", "chapter", "chunk",
# "chapter_done", "skip", "done".
EventFn = Callable[[str, dict], None]

MANIFEST_NAME = ".audiobook.json"


@dataclass
class Result:
    """What one `convert()` call did, and where the book stands overall.

    The distinction that matters: `files` is this run, while `completed` and
    `total` describe every run so far. Converting a 384-chapter boxed set five
    chapters at a time needs both - "5 done now" and "40 of 384 overall".
    """

    out_dir: Path
    book: Book
    files: list[Path]          # what this run produced or skipped
    single_file: Path | None
    seconds: float
    skipped: int
    completed: int             # chapters finished across every run so far
    total: int                 # chapters in the book

    @property
    def remaining(self) -> int:
        """Chapters still to convert. Clamped: never reports a negative."""
        return max(self.total - self.completed, 0)


def safe_name(text: str, limit: int = 60) -> str:
    """Turn a chapter title into something Windows will accept as a filename."""

    text = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "", text).strip(" .")
    text = re.sub(r"\s+", " ", text)
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0]
    return text or "chapter"


def default_out_dir(pdf_path: Path) -> Path:
    """Output folder beside the PDF: "Book.pdf" -> "Book (audiobook)/".

    Next to the source rather than in a central library, so the audio is
    findable by whoever went looking for the book.
    """
    return pdf_path.parent / f"{pdf_path.stem} (audiobook)"


def _chapter_filename(ch: Chapter, ext: str) -> str:
    """Zero-padded index first, so any player sorts chapters correctly."""
    return f"{ch.index:02d} - {safe_name(ch.title)}.{ext}"


def load_manifest(out_dir: Path) -> dict:
    """Read the record of what has already been converted in this folder."""

    path = Path(out_dir) / MANIFEST_NAME
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"chapters": {}}


def is_finished(ch: Chapter, manifest: dict, out_dir: Path, ext: str,
                voice: str | None = None) -> bool:
    """True when this chapter's audio is on disk and still matches the text.

    Changing the voice or the page range changes the answer, so a rerun
    correctly redoes work that is no longer current.
    """

    record = manifest.get("chapters", {}).get(str(ch.index))
    if not record:
        return False
    target = Path(out_dir) / _chapter_filename(ch, ext)
    if not (target.exists() and target.stat().st_size > 0):
        return False
    if record.get("words") != ch.word_count:
        return False
    return voice is None or record.get("voice") == voice


def convert(
    pdf_path: str | Path,
    out_dir: str | Path | None = None,
    *,
    engine: str = "edge",
    voice: str = "en-US-AriaNeural",
    rate: str = "+0%",
    pitch: str = "+0Hz",
    volume: str = "+0%",
    first_page: int = 1,
    last_page: int | None = None,
    pages_per_part: int = 25,
    use_toc: bool = True,
    toc_level: str | int = "auto",
    clean: bool = True,
    password: str | None = None,
    chapters: Iterable[int] | None = None,
    limit: int | None = None,
    jobs: int = DEFAULT_CONCURRENCY,
    single_file: bool = False,
    resume: bool = True,
    speak_titles: bool = True,
    on_event: EventFn | None = None,
) -> Result:
    """Convert `pdf_path` into one audio file per chapter under `out_dir`.

    `chapters` picks specific chapter numbers; `limit` converts only that many
    of the ones still outstanding. Together they make it practical to work
    through a long book a chapter at a time.
    """

    started = time.time()
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise FileNotFoundError(f"No such PDF: {pdf_path}")

    out_dir = Path(out_dir) if out_dir else default_out_dir(pdf_path)
    out_dir.mkdir(parents=True, exist_ok=True)

    def emit(kind: str, **payload) -> None:
        """Report progress, if anyone is listening. Never fails the run."""
        if on_event:
            on_event(kind, payload)

    book = load_pdf(pdf_path, first_page=first_page, last_page=last_page,
                    pages_per_part=pages_per_part, use_toc=use_toc,
                    toc_level=toc_level, clean=clean, password=password)
    if not book.title:
        book.title = pdf_path.stem

    tts = get_engine(engine, voice=voice, rate=rate, pitch=pitch,
                     volume=volume, concurrency=jobs)
    try:
        rate_pct = float(str(rate).strip().rstrip("%") or 0)
    except ValueError:
        rate_pct = 0.0

    manifest_path = out_dir / MANIFEST_NAME
    manifest = load_manifest(out_dir) if resume else {"chapters": {}}
    manifest["title"] = book.title
    manifest["source"] = str(pdf_path)

    def done_already(ch: Chapter) -> bool:
        """True when this chapter can be skipped on this run."""
        return resume and is_finished(ch, manifest, out_dir, tts.ext, voice)

    selected = list(book.chapters)
    if chapters is not None:
        wanted = set(chapters)
        selected = [c for c in selected if c.index in wanted]
        if not selected:
            available = f"{book.chapters[0].index}-{book.chapters[-1].index}"
            raise ValueError(
                f"No chapters matched {sorted(wanted)}. This book has chapters "
                f"{available}; run --dry-run to see the numbering."
            )
    if limit is not None:
        selected = [c for c in selected if not done_already(c)][:limit]

    emit("book", book=book, selected=selected)

    files: list[Path] = []
    skipped = 0

    for ch in selected:
        target = out_dir / _chapter_filename(ch, tts.ext)

        if done_already(ch):
            emit("skip", chapter=ch, path=target)
            files.append(target)
            skipped += 1
            continue

        emit("chapter", chapter=ch, path=target, total=len(selected))

        text = f"{ch.title}.\n\n{ch.text}" if speak_titles else ch.text
        partial = target.with_suffix(target.suffix + ".part")
        chapter_started = time.time()
        tts.synthesize(text, partial,
                       on_progress=lambda done, total, c=ch: emit(
                           "chunk", chapter=c, done=done, total=total))
        partial.replace(target)
        took = time.time() - chapter_started

        files.append(target)
        # `seconds` feeds the build-time estimate, which calibrates itself to
        # whatever throughput this machine and connection actually manage.
        manifest["chapters"][str(ch.index)] = {
            "file": target.name, "words": ch.word_count, "voice": voice,
            "title": ch.title, "seconds": round(took, 2), "engine": engine,
            "rate_pct": rate_pct, "jobs": jobs,
        }
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        emit("chapter_done", chapter=ch, path=target)

    # The playlist and the merged file describe everything converted so far,
    # not just this run - so working through a book one chapter at a time
    # still leaves you with a playable whole.
    current = _finished_chapters(book, manifest, out_dir, tts.ext, voice)
    _write_playlist(out_dir, book.title, current)

    merged: Path | None = None
    if single_file and current:
        merged = out_dir / f"{safe_name(book.title, 80)}.{tts.ext}"
        concat_files([path for _, path in current], merged)

    result = Result(out_dir=out_dir, book=book, files=files, single_file=merged,
                    seconds=time.time() - started, skipped=skipped,
                    completed=len(current), total=len(book.chapters))
    emit("done", result=result)
    return result


def _finished_chapters(book: Book, manifest: dict, out_dir: Path, ext: str,
                       voice: str) -> list[tuple[Chapter, Path]]:
    """Chapters whose audio is on disk AND still matches the current text.

    A file left over from a different page range or a different voice is
    deliberately not counted: it will be regenerated, so calling it done would
    overstate progress and would poison a merged file.
    """

    pairs = []
    for ch in book.chapters:
        if is_finished(ch, manifest, out_dir, ext, voice):
            pairs.append((ch, out_dir / _chapter_filename(ch, ext)))
    return pairs


def _write_playlist(out_dir: Path, title: str,
                    chapters: list[tuple[Chapter, Path]]) -> None:
    """An .m3u so any player queues the finished chapters in the right order."""

    lines = ["#EXTM3U", f"#PLAYLIST:{title}"]
    for ch, path in chapters:
        lines.append(f"#EXTINF:{int(ch.est_minutes * 60)},{ch.title}")
        lines.append(path.name)
    (out_dir / "playlist.m3u").write_text("\n".join(lines) + "\n", encoding="utf-8")
