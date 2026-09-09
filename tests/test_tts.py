"""Chunking, rate parsing, and the back-pressure behaviour around the service."""

from __future__ import annotations

import asyncio
import wave

import pytest

from pdf_audiobook.tts import (
    DEFAULT_CHUNK_CHARS,
    AdaptiveLimiter,
    TTSError,
    _as_signed,
    chunk_text,
    concat_files,
    get_engine,
    is_rate_limited,
    merge_wavs,
)


class TestChunkText:
    def test_empty_text_yields_nothing(self):
        assert chunk_text("") == []
        assert chunk_text("   \n\n  ") == []

    def test_short_text_is_a_single_chunk(self):
        assert chunk_text("One short paragraph.") == ["One short paragraph."]

    def test_respects_the_size_limit(self):
        text = "\n\n".join("A sentence of some length here." for _ in range(200))
        chunks = chunk_text(text, max_chars=200)
        assert chunks
        assert all(len(c) <= 200 for c in chunks)

    def test_never_splits_mid_sentence_when_it_can_avoid_it(self):
        text = "\n\n".join(f"Sentence number {i} ends here." for i in range(20))
        for chunk in chunk_text(text, max_chars=120):
            assert chunk.strip().endswith(".")

    def test_a_monster_paragraph_falls_back_to_sentences(self):
        para = " ".join(f"Sentence {i} is here." for i in range(100))
        chunks = chunk_text(para, max_chars=100)
        assert len(chunks) > 1
        assert all(len(c) <= 100 for c in chunks)

    def test_text_with_no_punctuation_is_hard_split_rather_than_dropped(self):
        # A wall of unpunctuated text must still be spoken, not silently lost.
        text = "word " * 500
        chunks = chunk_text(text, max_chars=100)
        assert all(len(c) <= 100 for c in chunks)
        assert sum(c.count("word") for c in chunks) == 500

    def test_no_text_is_lost(self):
        text = "\n\n".join(f"Paragraph {i} with some words." for i in range(50))
        rejoined = " ".join(chunk_text(text, max_chars=150)).split()
        assert rejoined == text.split()

    def test_default_chunk_size_is_sane(self):
        assert 500 < DEFAULT_CHUNK_CHARS < 10_000


class TestRateParsing:
    @pytest.mark.parametrize(
        ("value", "unit", "expected"),
        [
            (10, "%", "+10%"),
            ("10", "%", "+10%"),
            ("+10%", "%", "+10%"),
            ("-5%", "%", "-5%"),
            ("", "%", "+0%"),
            ("+", "%", "+0%"),
            ("+0Hz", "Hz", "+0Hz"),
            (-3, "Hz", "-3Hz"),
        ],
    )
    def test_always_signed_and_suffixed(self, value, unit, expected):
        assert _as_signed(value, unit) == expected


class TestRateLimitDetection:
    def test_reads_a_status_attribute(self):
        exc = Exception("nope")
        exc.status = 429
        assert is_rate_limited(exc)

    def test_reads_a_status_code_attribute(self):
        exc = Exception("nope")
        exc.status_code = 429
        assert is_rate_limited(exc)

    @pytest.mark.parametrize(
        "message",
        ["HTTP 429 returned", "Too Many Requests", "rate limit exceeded", "Throttled."],
    )
    def test_reads_the_message(self, message):
        assert is_rate_limited(Exception(message))

    def test_an_ordinary_failure_is_not_a_refusal(self):
        # Backing off on every error would turn one dropped connection into a
        # permanently crippled worker pool.
        assert not is_rate_limited(ConnectionError("connection reset"))


class TestAdaptiveLimiter:
    def test_starts_at_the_requested_limit(self):
        assert AdaptiveLimiter(4).active_limit == 4

    def test_back_off_retires_a_permit_permanently(self):
        async def scenario():
            limiter = AdaptiveLimiter(4)
            await limiter.back_off()
            return limiter.active_limit

        assert asyncio.run(scenario()) == 3

    def test_never_shrinks_below_the_minimum(self):
        async def scenario():
            limiter = AdaptiveLimiter(3, minimum=1)
            for _ in range(10):
                await limiter.back_off()
            return limiter.active_limit

        assert asyncio.run(scenario()) == 1

    def test_acts_as_a_semaphore(self):
        async def scenario():
            limiter = AdaptiveLimiter(2)
            peak = 0
            live = 0

            async def worker():
                nonlocal peak, live
                async with limiter:
                    live += 1
                    peak = max(peak, live)
                    await asyncio.sleep(0.01)
                    live -= 1

            await asyncio.gather(*(worker() for _ in range(8)))
            return peak

        assert asyncio.run(scenario()) <= 2


class TestMerging:
    def test_merged_wav_holds_every_frame(self, wav_parts, tmp_path):
        out = tmp_path / "joined.wav"
        merge_wavs(wav_parts, out)

        total = 0
        for part in wav_parts:
            with wave.open(str(part), "rb") as handle:
                total += handle.getnframes()
        with wave.open(str(out), "rb") as handle:
            assert handle.getnframes() == total

    def test_concat_dispatches_on_extension(self, wav_parts, tmp_path):
        out = tmp_path / "joined.wav"
        assert concat_files(wav_parts, out) == out
        assert out.stat().st_size > 0

    def test_mp3_concat_is_a_byte_append(self, tmp_path):
        parts = []
        for i in range(3):
            path = tmp_path / f"{i}.mp3"
            path.write_bytes(bytes([i]) * 100)
            parts.append(path)

        out = tmp_path / "joined.mp3"
        concat_files(parts, out)
        assert out.read_bytes() == b"\x00" * 100 + b"\x01" * 100 + b"\x02" * 100

    def test_merging_nothing_is_an_error(self, tmp_path):
        with pytest.raises(TTSError, match="Nothing to merge"):
            concat_files([], tmp_path / "out.mp3")


class TestEngineFactory:
    def test_returns_the_named_engine(self):
        assert get_engine("edge").name == "edge"
        assert get_engine("offline").name == "offline"

    def test_unknown_engine_lists_the_choices(self):
        with pytest.raises(ValueError, match="edge"):
            get_engine("nonsense")

    def test_concurrency_is_clamped(self):
        assert get_engine("edge", concurrency=999).concurrency <= 16
        assert get_engine("edge", concurrency=0).concurrency == 1
