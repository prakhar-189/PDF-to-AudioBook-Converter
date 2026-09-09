"""Orchestration: filenames, the resume manifest, and end-to-end conversion.

Conversion is driven with a stub engine rather than the real voice service, so
these run offline and in milliseconds. `eval.py --audio` is what exercises real
synthesis.
"""

from __future__ import annotations

import json

import pytest

from pdf_audiobook import audiobook
from pdf_audiobook.audiobook import (
    MANIFEST_NAME,
    Result,
    convert,
    default_out_dir,
    is_finished,
    load_manifest,
    safe_name,
)
from pdf_audiobook.extract import Chapter


class StubEngine:
    """Writes a deterministic file instead of calling Microsoft."""

    ext = "mp3"
    name = "stub"

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.calls: list[str] = []

    def synthesize(self, text, out_path, on_progress=None):
        self.calls.append(text)
        out_path.write_bytes(b"\xff\xfb" + text.encode("utf-8")[:64])
        if on_progress:
            on_progress(1, 1)
        return out_path


@pytest.fixture
def stub_engine(monkeypatch):
    engine = StubEngine()
    monkeypatch.setattr(audiobook, "get_engine", lambda name, **kw: engine)
    return engine


class TestSafeName:
    @pytest.mark.parametrize("bad", ['a<b>c:d"e/f\\g|h?i*j'])
    def test_strips_characters_windows_rejects(self, bad):
        out = safe_name(bad)
        assert not set(out) & set('<>:"/\\|?*')

    def test_collapses_whitespace(self):
        assert safe_name("Chapter    One\n") == "Chapter One"

    def test_truncates_on_a_word_boundary(self):
        out = safe_name("word " * 40, limit=20)
        assert len(out) <= 20
        assert not out.endswith(" ")

    def test_never_returns_an_empty_filename(self):
        assert safe_name("***") == "chapter"
        assert safe_name("") == "chapter"


class TestManifest:
    def test_missing_manifest_reads_as_empty(self, tmp_path):
        assert load_manifest(tmp_path) == {"chapters": {}}

    def test_corrupt_manifest_does_not_crash_the_run(self, tmp_path):
        # A half-written manifest from a Ctrl+C must not make the book
        # unconvertible - the worst it should cost is redoing the work.
        (tmp_path / MANIFEST_NAME).write_text("{not json", encoding="utf-8")
        assert load_manifest(tmp_path) == {"chapters": {}}


class TestIsFinished:
    @pytest.fixture
    def chapter(self):
        return Chapter(1, "One", 0, 1, text="one two three")

    def _manifest(self, chapter, **overrides):
        record = {"words": chapter.word_count, "voice": "aria"}
        record.update(overrides)
        return {"chapters": {str(chapter.index): record}}

    def test_true_when_audio_exists_and_matches(self, tmp_path, chapter):
        (tmp_path / "01 - One.mp3").write_bytes(b"audio")
        assert is_finished(chapter, self._manifest(chapter), tmp_path, "mp3", "aria")

    def test_false_without_a_record(self, tmp_path, chapter):
        (tmp_path / "01 - One.mp3").write_bytes(b"audio")
        assert not is_finished(chapter, {"chapters": {}}, tmp_path, "mp3", "aria")

    def test_false_when_the_file_is_gone(self, tmp_path, chapter):
        assert not is_finished(chapter, self._manifest(chapter), tmp_path, "mp3", "aria")

    def test_false_when_the_file_is_empty(self, tmp_path, chapter):
        # A zero-byte file is exactly what a silently failed synthesis leaves
        # behind, and it looks like a finished chapter to a directory listing.
        (tmp_path / "01 - One.mp3").write_bytes(b"")
        assert not is_finished(chapter, self._manifest(chapter), tmp_path, "mp3", "aria")

    def test_false_when_the_text_changed(self, tmp_path, chapter):
        (tmp_path / "01 - One.mp3").write_bytes(b"audio")
        manifest = self._manifest(chapter, words=999)
        assert not is_finished(chapter, manifest, tmp_path, "mp3", "aria")

    def test_false_when_the_voice_changed(self, tmp_path, chapter):
        # Otherwise you end up with a book narrated by two different people.
        (tmp_path / "01 - One.mp3").write_bytes(b"audio")
        assert not is_finished(chapter, self._manifest(chapter), tmp_path, "mp3", "ryan")


class TestConvert:
    def test_writes_one_file_per_chapter_and_a_playlist(self, novel_pdf, tmp_path, stub_engine):
        out = tmp_path / "out"
        result = convert(novel_pdf, out, engine="stub", voice="aria")

        assert isinstance(result, Result)
        assert result.total == 3
        assert result.completed == 3
        assert result.remaining == 0
        assert len(list(out.glob("*.mp3"))) == 3
        assert (out / "playlist.m3u").exists()

    def test_playlist_lists_chapters_in_order(self, novel_pdf, tmp_path, stub_engine):
        out = tmp_path / "out"
        convert(novel_pdf, out, engine="stub", voice="aria")
        lines = (out / "playlist.m3u").read_text(encoding="utf-8").splitlines()

        assert lines[0] == "#EXTM3U"
        files = [ln for ln in lines if ln.endswith(".mp3")]
        assert files == sorted(files)

    def test_speaks_the_chapter_title_first(self, novel_pdf, tmp_path, stub_engine):
        convert(novel_pdf, tmp_path / "out", engine="stub", voice="aria")
        assert stub_engine.calls[0].startswith("Chapter 1.")

    def test_speak_titles_can_be_turned_off(self, novel_pdf, tmp_path, stub_engine):
        convert(novel_pdf, tmp_path / "out", engine="stub", voice="aria", speak_titles=False)
        assert not stub_engine.calls[0].startswith("Chapter 1.")

    def test_a_second_run_skips_finished_chapters(self, novel_pdf, tmp_path, stub_engine):
        out = tmp_path / "out"
        convert(novel_pdf, out, engine="stub", voice="aria")
        before = len(stub_engine.calls)

        again = convert(novel_pdf, out, engine="stub", voice="aria")
        assert len(stub_engine.calls) == before      # nothing re-synthesised
        assert again.skipped == 3

    def test_changing_the_voice_redoes_the_work(self, novel_pdf, tmp_path, stub_engine):
        out = tmp_path / "out"
        convert(novel_pdf, out, engine="stub", voice="aria")
        before = len(stub_engine.calls)

        again = convert(novel_pdf, out, engine="stub", voice="ryan")
        assert len(stub_engine.calls) > before
        assert again.skipped == 0

    def test_limit_converts_only_the_next_few(self, novel_pdf, tmp_path, stub_engine):
        out = tmp_path / "out"
        result = convert(novel_pdf, out, engine="stub", voice="aria", limit=1)
        assert result.completed == 1
        assert result.remaining == 2

    def test_specific_chapters_can_be_selected(self, novel_pdf, tmp_path, stub_engine):
        out = tmp_path / "out"
        convert(novel_pdf, out, engine="stub", voice="aria", chapters=[2])
        assert [p.name[:2] for p in sorted(out.glob("*.mp3"))] == ["02"]

    def test_an_impossible_chapter_number_explains_the_range(self, novel_pdf, tmp_path,
                                                             stub_engine):
        with pytest.raises(ValueError, match="run --dry-run"):
            convert(novel_pdf, tmp_path / "out", engine="stub", voice="aria", chapters=[99])

    def test_single_file_merges_everything(self, novel_pdf, tmp_path, stub_engine):
        out = tmp_path / "out"
        result = convert(novel_pdf, out, engine="stub", voice="aria", single_file=True)
        assert result.single_file is not None
        assert result.single_file.exists()

    def test_manifest_records_what_the_estimate_needs(self, novel_pdf, tmp_path, stub_engine):
        out = tmp_path / "out"
        convert(novel_pdf, out, engine="stub", voice="aria", jobs=4)
        record = json.loads((out / MANIFEST_NAME).read_text(encoding="utf-8"))["chapters"]["1"]

        # These four fields are exactly what observed_speedup() calibrates on.
        for key in ("words", "seconds", "jobs", "engine"):
            assert key in record

    def test_partial_files_are_not_left_behind(self, novel_pdf, tmp_path, stub_engine):
        out = tmp_path / "out"
        convert(novel_pdf, out, engine="stub", voice="aria")
        assert list(out.glob("*.part")) == []

    def test_missing_pdf_is_reported_clearly(self, tmp_path, stub_engine):
        with pytest.raises(FileNotFoundError):
            convert(tmp_path / "nope.pdf", tmp_path / "out", engine="stub")

    def test_events_are_emitted_for_progress_reporting(self, novel_pdf, tmp_path, stub_engine):
        seen = []
        convert(novel_pdf, tmp_path / "out", engine="stub", voice="aria",
                on_event=lambda kind, payload: seen.append(kind))

        assert seen[0] == "book"
        assert seen[-1] == "done"
        assert seen.count("chapter_done") == 3


class TestDefaultOutDir:
    def test_sits_next_to_the_pdf(self, tmp_path):
        pdf = tmp_path / "My Book.pdf"
        assert default_out_dir(pdf) == tmp_path / "My Book (audiobook)"
