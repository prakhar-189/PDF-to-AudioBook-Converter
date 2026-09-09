"""Text-to-speech engines.

Two of them:

* `EdgeEngine`  - Microsoft's neural voices via `edge-tts`. Free, no API key,
  needs an internet connection. This is what you want for a book you will
  actually listen to for hours.
* `OfflineEngine` - `pyttsx3` driving the voices already installed on your OS.
  Works on a plane. Sounds like a 2005 GPS unit.

Both expose the same `synthesize(text, out_path, on_progress)` call and write a
single audio file per chapter.
"""

from __future__ import annotations

import asyncio
import random
import re
import tempfile
import wave
from pathlib import Path
from typing import Callable

ProgressFn = Callable[[int, int], None]

# Roughly one paragraph of speech. Small enough that a dropped connection
# costs seconds rather than a whole chapter.
DEFAULT_CHUNK_CHARS = 2200

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|(?<=[.!?][\"')\]])\s+")


def chunk_text(text: str, max_chars: int = DEFAULT_CHUNK_CHARS) -> list[str]:
    """Split text into TTS-sized pieces, never mid-sentence."""
    chunks: list[str] = []
    buf = ""

    def flush() -> None:
        nonlocal buf
        if buf.strip():
            chunks.append(buf.strip())
        buf = ""

    for paragraph in text.split("\n\n"):
        paragraph = paragraph.strip()
        if not paragraph:
            continue

        if len(buf) + len(paragraph) + 2 <= max_chars:
            buf = f"{buf}\n\n{paragraph}" if buf else paragraph
            continue

        flush()
        if len(paragraph) <= max_chars:
            buf = paragraph
            continue

        # A single monster paragraph: fall back to sentence granularity.
        for sentence in _SENTENCE_SPLIT.split(paragraph):
            sentence = sentence.strip()
            if not sentence:
                continue
            if len(buf) + len(sentence) + 1 > max_chars:
                flush()
            # Still too long (no sentence punctuation at all) - hard split.
            while len(sentence) > max_chars:
                chunks.append(sentence[:max_chars])
                sentence = sentence[max_chars:]
            buf = f"{buf} {sentence}".strip()

    flush()
    return chunks


class TTSError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# neural voices (online)
# --------------------------------------------------------------------------

# How many synthesis requests are in flight at once. Four is deliberately
# modest: the service is free and shared, and most of the available speed-up
# is already there by the time you reach four.
DEFAULT_CONCURRENCY = 4
MAX_CONCURRENCY = 16

BASE_BACKOFF = 1.5          # seconds, doubled each attempt
RATE_LIMIT_BACKOFF = 5.0    # a refusal deserves a longer pause than a blip
MAX_BACKOFF = 60.0

RATE_LIMIT_HINTS = ("429", "too many requests", "rate limit", "throttl")


def is_rate_limited(exc: Exception) -> bool:
    """Did the service refuse us for asking too often, rather than just fail?"""
    for attr in ("status", "code", "status_code"):
        if getattr(exc, attr, None) == 429:
            return True
    return any(hint in str(exc).lower() for hint in RATE_LIMIT_HINTS)


class AdaptiveLimiter:
    """A semaphore that shrinks when the service pushes back.

    Permits are retired permanently rather than handed back. If the service
    has said we are asking for too much, the right response for the rest of
    the run is to ask for less - not to return to the same rate and be
    refused all over again.
    """

    def __init__(self, limit: int, minimum: int = 1) -> None:
        self.limit = max(1, limit)
        self.minimum = minimum
        self._sem = asyncio.Semaphore(self.limit)
        self._retired: list = []

    async def __aenter__(self) -> AdaptiveLimiter:
        await self._sem.acquire()
        return self

    async def __aexit__(self, *exc_info) -> None:
        self._sem.release()

    @property
    def active_limit(self) -> int:
        return self.limit - len(self._retired)

    async def back_off(self) -> None:
        if self.active_limit <= self.minimum:
            return
        # Holding a permit forever is how an asyncio.Semaphore gets smaller.
        # The task is kept referenced so it is not collected mid-acquire.
        self._retired.append(asyncio.create_task(self._sem.acquire()))


class EdgeEngine:
    """Microsoft Edge neural voices. MP3 out, no API key, needs internet."""

    ext = "mp3"
    name = "edge"

    def __init__(self, voice: str = "en-US-AriaNeural", rate: str = "+0%",
                 pitch: str = "+0Hz", volume: str = "+0%", retries: int = 4,
                 concurrency: int = DEFAULT_CONCURRENCY) -> None:
        self.voice = voice
        self.rate = _as_signed(rate, "%")
        self.pitch = _as_signed(pitch, "Hz")
        self.volume = _as_signed(volume, "%")
        self.retries = retries
        self.concurrency = max(1, min(int(concurrency), MAX_CONCURRENCY))

    @staticmethod
    def list_voices(language: str | None = None) -> list[dict]:
        import edge_tts

        voices = asyncio.run(edge_tts.list_voices())
        if language:
            language = language.lower()
            voices = [v for v in voices
                      if v["Locale"].lower().startswith(language)]
        return sorted(voices, key=lambda v: v["ShortName"])

    async def _synth_chunk(self, text: str, limiter: AdaptiveLimiter) -> bytes:
        """One chunk of text to MP3 bytes, with backoff and rate-limit care."""
        import edge_tts

        last_error: Exception | None = None
        for attempt in range(self.retries):
            try:
                async with limiter:
                    communicate = edge_tts.Communicate(
                        text, self.voice, rate=self.rate,
                        volume=self.volume, pitch=self.pitch,
                    )
                    buffer = bytearray()
                    async for packet in communicate.stream():
                        if packet["type"] == "audio":
                            buffer.extend(packet["data"])

                # A successful response carrying no audio raises nothing at
                # all, so it has to be caught deliberately. Left unchecked it
                # writes an empty file that looks like a finished chapter.
                if not buffer:
                    raise TTSError("the voice service returned no audio")
                return bytes(buffer)

            except Exception as exc:  # noqa: BLE001 - retried, then re-raised
                last_error = exc
                if attempt == self.retries - 1:
                    break
                if is_rate_limited(exc):
                    await limiter.back_off()
                    delay = RATE_LIMIT_BACKOFF * (2 ** attempt)
                else:
                    delay = BASE_BACKOFF * (2 ** attempt)
                # Jitter, so workers that failed together do not all return at
                # the same instant and fail together again.
                await asyncio.sleep(
                    min(delay, MAX_BACKOFF) * (1 + random.random() * 0.3))

        raise TTSError(
            f"Speech synthesis failed after {self.retries} attempts: {last_error}\n"
            "Check your internet connection, and try `pip install -U edge-tts` - "
            "Microsoft changes the endpoint occasionally and older versions break."
        ) from last_error

    async def _run(self, chunks: list[str], out_path: Path,
                   on_progress: ProgressFn | None) -> None:
        limiter = AdaptiveLimiter(self.concurrency)
        completed = 0

        async def one(text: str) -> bytes:
            nonlocal completed
            data = await self._synth_chunk(text, limiter)
            completed += 1
            if on_progress:
                on_progress(completed, len(chunks))
            return data

        # Ordered windows rather than one big gather. MP3 frames must be
        # written in sequence, and this bounds how much finished audio sits in
        # memory while one slow chunk ahead of it is still retrying.
        with open(out_path, "wb") as handle:
            for start in range(0, len(chunks), self.concurrency):
                window = chunks[start:start + self.concurrency]
                for data in await asyncio.gather(*(one(c) for c in window)):
                    handle.write(data)

    def synthesize(self, text: str, out_path: Path,
                   on_progress: ProgressFn | None = None) -> Path:
        chunks = chunk_text(text)
        if not chunks:
            raise TTSError("Nothing to speak.")
        asyncio.run(self._run(chunks, out_path, on_progress))
        return out_path


def _as_signed(value: str | int | float, unit: str) -> str:
    """edge-tts wants '+10%' / '-5Hz'; accept 10, '10', '+10%' and friends."""
    text = str(value).strip()
    text = text.removesuffix(unit) if text.endswith(unit) else text.rstrip("%Hz")
    if not text or text in "+-":
        text = "0"
    number = float(text)
    return f"{number:+.0f}{unit}"


# --------------------------------------------------------------------------
# system voices (offline)
# --------------------------------------------------------------------------

class OfflineEngine:
    """pyttsx3 over the OS speech engine. WAV out, works with no internet."""

    ext = "wav"
    name = "offline"

    def __init__(self, voice: str | None = None, rate: str | int = "+0%",
                 **_ignored) -> None:
        self.voice = voice
        # Reuse the same "+15%" vocabulary as the neural engine and convert it
        # to pyttsx3's words-per-minute.
        self.rate_pct = float(_as_signed(rate, "%").rstrip("%"))

    @staticmethod
    def list_voices(language: str | None = None) -> list[dict]:
        import pyttsx3

        engine = pyttsx3.init()
        try:
            voices = [{"ShortName": v.id, "FriendlyName": v.name,
                       "Locale": (v.languages[0] if v.languages else ""),
                       "Gender": getattr(v, "gender", "") or ""}
                      for v in engine.getProperty("voices")]
        finally:
            engine.stop()
        if language:
            language = language.lower()
            voices = [v for v in voices if language in str(v["Locale"]).lower()
                      or language in v["FriendlyName"].lower()]
        return voices

    def _new_engine(self):
        import pyttsx3

        engine = pyttsx3.init()
        base = engine.getProperty("rate") or 200
        engine.setProperty("rate", int(base * (1 + self.rate_pct / 100.0)))

        # `--voice` defaults to a neural voice name, which means nothing to the
        # OS engine. Only set it if it really matches an installed voice;
        # otherwise leave the system default in place.
        if self.voice:
            available = {v.id: v.id for v in engine.getProperty("voices")}
            available.update({v.name: v.id for v in engine.getProperty("voices")})
            match = available.get(self.voice)
            if match is None:
                wanted = self.voice.lower()
                match = next((vid for key, vid in available.items()
                              if wanted in key.lower()), None)
            if match:
                engine.setProperty("voice", match)
        return engine

    def synthesize(self, text: str, out_path: Path,
                   on_progress: ProgressFn | None = None) -> Path:
        chunks = chunk_text(text, max_chars=6000)
        if not chunks:
            raise TTSError("Nothing to speak.")

        with tempfile.TemporaryDirectory() as tmp:
            parts: list[Path] = []
            for i, chunk in enumerate(chunks, start=1):
                part = Path(tmp) / f"{i:05d}.wav"
                engine = self._new_engine()
                try:
                    engine.save_to_file(chunk, str(part))
                    engine.runAndWait()
                finally:
                    engine.stop()
                if not part.exists() or part.stat().st_size == 0:
                    raise TTSError(
                        "The offline voice produced no audio. On Windows, check that "
                        "a voice is installed under Settings > Time & language > Speech."
                    )
                parts.append(part)
                if on_progress:
                    on_progress(i, len(chunks))
            merge_wavs(parts, out_path)
        return out_path


def merge_wavs(parts: list[Path], out_path: Path) -> Path:
    """Join WAV files that share a format, without needing ffmpeg."""
    with wave.open(str(parts[0]), "rb") as first:
        params = first.getparams()

    with wave.open(str(out_path), "wb") as out:
        out.setparams(params)
        for part in parts:
            with wave.open(str(part), "rb") as src:
                out.writeframes(src.readframes(src.getnframes()))
    return out_path


def concat_files(parts: list[Path], out_path: Path) -> Path:
    """Merge finished chapter files into one long audio file.

    MP3s from a single voice share encoder settings, so a byte-level append
    plays back correctly; WAVs need their header rewritten.
    """
    if not parts:
        raise TTSError("Nothing to merge.")
    if parts[0].suffix.lower() == ".wav":
        return merge_wavs(parts, out_path)

    with open(out_path, "wb") as out:
        for part in parts:
            out.write(part.read_bytes())
    return out_path


def get_engine(name: str, **kwargs):
    engines = {"edge": EdgeEngine, "offline": OfflineEngine}
    if name not in engines:
        raise ValueError(f"Unknown engine {name!r}. Choose from: {', '.join(engines)}")
    return engines[name](**kwargs)
