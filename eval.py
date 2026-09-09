"""Accuracy tests for the PDF to audiobook pipeline.

    python eval.py              # text + structure (fast, no network)
    python eval.py --audio      # also generate real audio and check it
    python eval.py --keep       # leave the generated files for inspection

The point of this file is to answer one question honestly: **does the text you
hear match the text in the PDF?**

That is measurable because the test PDFs are built here, so the exact body text
that went in is known. Everything the extractor has to survive - running
headers, footers, page numbers, words hyphenated across line breaks, citation
markers, hard-wrapped lines - is deliberately baked in, and the extracted
narration is compared against the known-good original word for word.

The pass mark is 95%.
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import random
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from pdf_audiobook.audiobook import convert
from pdf_audiobook.extract import load_pdf
from pdf_audiobook.tts import AdaptiveLimiter, EdgeEngine, is_rate_limited

PASS_MARK = 95.0

# --------------------------------------------------------------------------
# building test documents with known contents
# --------------------------------------------------------------------------

PROSE = [
    "The harbour was quiet in the hour before dawn and the water lay flat as glass.",
    "Gulls turned overhead without calling, and the fishing boats sat where their",
    "owners had left them, ropes slack, decks wet with the night's condensation.",
    "Nothing about the morning suggested that the day would be remembered at all.",
    "A single lamp burned at the end of the pier, though no one could say who had",
    "lit it, or when, or why it had not been extinguished with all the others.",
    "By the time the sun cleared the eastern headland the lamp had burned out and",
    "the harbourmaster had begun his rounds, counting hulls against his register.",
    "He found one more vessel than the register allowed for, which had never once",
    "happened in the eleven years he had held the post, and he counted them again.",
]

# Words split across a line break in the PDF. The extractor must rejoin them.
HYPHENATED = [("under", "standing"), ("harbour", "master"), ("extra", "ordinary")]


@dataclass
class Truth:
    """The text put into a generated PDF, kept so we can score what comes out.

    The whole harness rests on this. Because each document is generated rather
    than found, the exact body text is known, and "did extraction work" becomes
    a measurable comparison instead of a judgement call.
    """
    """What the PDF really says, recorded as it is written."""

    name: str
    body: list[str] = field(default_factory=list)      # paragraphs of real prose
    chapters: list[str] = field(default_factory=list)  # expected chapter titles
    noise: list[str] = field(default_factory=list)     # must NOT be narrated
    rejoined: list[str] = field(default_factory=list)  # words that were hyphen-split

    @property
    def text(self) -> str:
        """The full expected body text, as one string."""
        return " ".join(self.body)


def _draw(page, lines, top=90, size=11, leading=16):
    """Write lines onto a page at fixed leading, imitating typeset prose."""

    y = top
    for line in lines:
        page.insert_text((60, y), line, fontsize=size)
        y += leading
    return y


def build_novel(path: Path) -> Truth:
    """A normal book: contents page, running heads, page numbers, 3 chapters."""

    truth = Truth("novel with table of contents")
    doc = pymupdf.open()
    titles = ["Chapter One", "Chapter Two", "Chapter Three"]
    truth.chapters = list(titles)
    truth.noise = ["THE QUIET HARBOUR", "GEORGE EXAMPLE"]

    page = doc.new_page()
    page.insert_text((60, 200), "The Quiet Harbour", fontsize=26)
    page.insert_text((60, 240), "by George Example", fontsize=13)

    toc = []
    page_no = 2
    for title in titles:
        toc.append([1, title, page_no])
        for i in range(2):
            page = doc.new_page()
            # running header and footer on every page, plus a page number
            page.insert_text((60, 40), "THE QUIET HARBOUR", fontsize=8)
            page.insert_text((430, 40), "GEORGE EXAMPLE", fontsize=8)
            page.insert_text((300, 780), str(page_no), fontsize=9)

            lines = list(PROSE)
            if i == 0:
                lines = [title, *lines]
            # split a word across a line break, the way justified text does
            head, tail = HYPHENATED[len(truth.rejoined) % len(HYPHENATED)]
            lines.append(f"The {head}-")
            lines.append(f"{tail} of the tide was complete.")
            truth.rejoined.append(head + tail)
            truth.body.append(" ".join(PROSE))
            truth.body.append(f"The {head + tail} of the tide was complete.")

            _draw(page, lines)
            page_no += 1

    doc.set_toc(toc)
    doc.set_metadata({"title": "The Quiet Harbour", "author": "George Example"})
    doc.save(path)
    doc.close()
    return truth


def build_paper(path: Path) -> Truth:
    """An academic-looking PDF: citations, footnote markers, no contents page."""

    truth = Truth("paper with citations, no outline")
    truth.noise = ["Journal of Imaginary Studies, Vol. 12"]
    doc = pymupdf.open()

    for n in range(4):
        page = doc.new_page()
        page.insert_text((60, 40), "Journal of Imaginary Studies, Vol. 12", fontsize=8)
        page.insert_text((300, 780), str(n + 1), fontsize=9)

        lines = []
        for i, sentence in enumerate(PROSE):
            # citation markers the narrator should not read out
            lines.append(sentence + (" [12]" if i % 3 == 0 else ""))
        truth.body.append(" ".join(PROSE))
        _draw(page, lines)

    doc.save(path)
    doc.close()
    return truth


def build_plain(path: Path) -> Truth:
    """No outline, no headers, no page numbers - just text on pages."""

    truth = Truth("plain pages, nothing to strip")
    doc = pymupdf.open()
    for _ in range(3):
        page = doc.new_page()
        _draw(page, PROSE)
        truth.body.append(" ".join(PROSE))
    doc.save(path)
    doc.close()
    return truth


BUILDERS = [build_novel, build_paper, build_plain]


# --------------------------------------------------------------------------
# comparing what was extracted against what was written
# --------------------------------------------------------------------------

def words_of(text: str) -> list[str]:
    """Lowercase word list, punctuation ignored - we score words, not commas."""
    return re.findall(r"[a-z0-9']+", text.lower())


def compare(expected: str, actual: str) -> tuple[float, float, int, int]:
    """Word-level recall and precision via longest-matching-block alignment."""
    want, got = words_of(expected), words_of(actual)
    if not want:
        return 0.0, 0.0, 0, 0

    matcher = difflib.SequenceMatcher(None, want, got, autojunk=False)
    matched = sum(block.size for block in matcher.get_matching_blocks())
    recall = matched / len(want) * 100
    precision = matched / len(got) * 100 if got else 0.0
    return recall, precision, len(want), len(got)


@dataclass
class Check:
    """One pass/fail assertion, with the label printed in the report."""

    name: str
    passed: bool
    detail: str = ""


def evaluate_document(builder, tmp: Path) -> tuple[Truth, float, list[Check]]:
    """Build one PDF, extract it, and score the result against the truth.

    Returns the truth, a word-level F1, and the individual checks - so the
    report shows both a number and which specific defences held.
    """

    path = tmp / f"{builder.__name__}.pdf"
    truth = builder(path)
    book = load_pdf(path)

    narration = " ".join(f"{c.title} {c.text}" for c in book.chapters)
    recall, precision, n_want, n_got = compare(truth.text, narration)
    f1 = 0.0 if recall + precision == 0 else 2 * recall * precision / (recall + precision)

    checks = [
        Check("body text recall", recall >= PASS_MARK, f"{recall:.1f}% of {n_want} words kept"),
        Check("narration precision", precision >= PASS_MARK,
              f"{precision:.1f}% of {n_got} narrated words are real body text"),
    ]

    # Nothing that only exists as page furniture should reach the narration.
    spoken = narration.lower()
    for noise in truth.noise:
        checks.append(Check(f"header removed: {noise[:28]}", noise.lower() not in spoken))

    stray = re.findall(r"(?m)^\s*\d{1,4}\s*$", narration)
    checks.append(Check("page numbers removed", not stray,
                        f"{len(stray)} stray number lines" if stray else ""))

    checks.append(Check("citation markers removed", "[12]" not in narration))

    for word in truth.rejoined:
        checks.append(Check(f"hyphen rejoined: {word}", word in spoken))

    if truth.chapters:
        found = [c.title for c in book.chapters]
        hit = sum(1 for t in truth.chapters if any(t in f for f in found))
        checks.append(Check("chapters detected", hit == len(truth.chapters),
                            f"{hit}/{len(truth.chapters)} titles found"))

    return truth, f1, checks


# --------------------------------------------------------------------------
# audio: did all of that text actually make it into the file?
# --------------------------------------------------------------------------

MPEG1_BITRATES = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0]
MPEG2_BITRATES = [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0]
SAMPLE_RATES = [44100, 48000, 32000, 0]


def mp3_duration(path: Path) -> float:
    """Seconds of audio, by walking the MPEG frame headers. No ffmpeg needed."""

    data = path.read_bytes()
    i, total = 0, 0.0
    while i < len(data) - 4:
        if data[i] == 0xFF and (data[i + 1] & 0xE0) == 0xE0:
            version = (data[i + 1] >> 3) & 3
            layer = (data[i + 1] >> 1) & 3
            rate = SAMPLE_RATES[(data[i + 2] >> 2) & 3]
            padding = (data[i + 2] >> 1) & 1
            if rate == 0 or layer != 1 or version == 1:
                i += 1
                continue
            table = MPEG1_BITRATES if version == 3 else MPEG2_BITRATES
            bitrate = table[(data[i + 2] >> 4) & 15]
            if bitrate == 0:
                i += 1
                continue
            if version == 2:
                rate //= 2
            elif version == 0:
                rate //= 4
            samples = 1152 if version == 3 else 576
            size = (samples // 8 * bitrate * 1000) // rate + padding
            if size <= 0:
                i += 1
                continue
            total += samples / rate
            i += size
        else:
            i += 1
    return total


def evaluate_audio(tmp: Path) -> list[Check]:
    """Convert a real document and confirm the audio holds all of the text.

    A silently dropped chunk is the failure that matters here, and it shows up
    as audio that is too short for the number of words it should contain.
    """

    path = tmp / "audio_case.pdf"
    build_plain(path)
    out = tmp / "audio_out"

    result = convert(path, out, rate="+0%", speak_titles=False, resume=False)
    checks: list[Check] = []

    checks.append(Check("audio produced for every chapter",
                        len(result.files) == len(result.book.chapters),
                        f"{len(result.files)}/{len(result.book.chapters)} files"))

    for chapter, audio in zip(result.book.chapters, result.files):
        if not (audio.exists() and audio.stat().st_size > 0):
            checks.append(Check(f"chapter {chapter.index} audio present", False, "missing or empty"))
            continue

        seconds = mp3_duration(audio)
        expected = chapter.est_minutes * 60
        ratio = seconds / expected if expected else 0.0
        # Generous window: narration pace varies. This is here to catch a
        # chunk that never made it, not to police speaking speed.
        ok = 0.7 <= ratio <= 1.4
        checks.append(Check(f"chapter {chapter.index} audio length",
                            ok, f"{seconds:.0f}s vs {expected:.0f}s expected ({ratio:.2f}x)"))

    return checks


# --------------------------------------------------------------------------
# concurrency: parallel synthesis must not reorder the book
# --------------------------------------------------------------------------

def evaluate_concurrency() -> list[Check]:
    """Chunks are synthesised in parallel but must be written in order.

    This drives the real assembly path with a stand-in synthesiser whose later
    chunks finish FIRST, so an ordering bug is certain to show rather than
    depending on timing luck. No network, so it is safe in CI.
    """

    checks: list[Check] = []
    chunks = [f"chunk-{i:03d}|" for i in range(37)]
    expected = "".join(chunks).encode()

    def assemble(concurrency: int, delay) -> bytes:
        """Reassemble chunks under a given concurrency and timing pattern."""
        engine = EdgeEngine(concurrency=concurrency)

        async def stand_in(text, limiter):
            """A synthesiser returning known bytes after a chosen delay."""
            await asyncio.sleep(delay(text))
            return text.encode()

        engine._synth_chunk = stand_in
        out = Path(tempfile.mkdtemp()) / "ordered.mp3"
        asyncio.run(engine._run(chunks, out, None))
        return out.read_bytes()

    def reversed_order(text):
        """Timing where later chunks finish first - the pathological case."""
        # Later chunks finish first, so a naive append-as-you-go would reorder audio.
        return 0.002 * (len(chunks) - chunks.index(text))

    checks.append(Check("order kept when later chunks finish first",
                        assemble(4, reversed_order) == expected))
    checks.append(Check("order kept with random timings, 8 workers",
                        assemble(8, lambda t: random.random() * 0.01) == expected))
    checks.append(Check("order kept with a single worker",
                        assemble(1, lambda t: 0.0) == expected))

    class Refused(Exception):
        """A stand-in for the service refusing us, to drive back-off."""
        status = 429

    checks.append(Check("rate limit seen via status attribute", is_rate_limited(Refused())))
    checks.append(Check("rate limit seen in the message",
                        is_rate_limited(Exception("Too Many Requests"))))
    checks.append(Check("ordinary failure is not mistaken for a rate limit",
                        not is_rate_limited(Exception("connection reset"))))

    async def shrink():
        """Drive the limiter with refusals and report the surviving width."""
        limiter = AdaptiveLimiter(4)
        await limiter.back_off()
        await limiter.back_off()
        await asyncio.sleep(0.05)
        shrunk = limiter.active_limit
        for _ in range(10):
            await limiter.back_off()
        await asyncio.sleep(0.05)
        return shrunk, limiter.active_limit

    shrunk, floor = asyncio.run(shrink())
    checks.append(Check("back-off shrinks the worker pool", shrunk == 2, f"4 -> {shrunk}"))
    checks.append(Check("back-off never shrinks below one", floor >= 1, f"floor {floor}"))
    return checks


# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    """Run the harness and return a process exit code (0 pass, 1 fail).

    Non-zero is what makes this a CI gate rather than a report.
    """

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--audio", action="store_true",
                        help="also generate real audio and check it (needs internet)")
    parser.add_argument("--keep", action="store_true",
                        help="keep the generated PDFs and audio for inspection")
    args = parser.parse_args(argv)

    tmp = Path(tempfile.mkdtemp(prefix="pdf_audiobook_eval_"))
    all_checks: list[Check] = []
    scores: list[float] = []

    try:
        print("=" * 74)
        print("PDF to audiobook - accuracy evaluation")
        print("=" * 74)

        for builder in BUILDERS:
            truth, f1, checks = evaluate_document(builder, tmp)
            scores.append(f1)
            all_checks.extend(checks)

            print(f"\n{truth.name}   (word-level F1 {f1:.1f}%)")
            for check in checks:
                mark = "PASS" if check.passed else "FAIL"
                detail = f"  - {check.detail}" if check.detail else ""
                print(f"   [{mark}] {check.name}{detail}")

        print("\nconcurrency")
        concurrency_checks = evaluate_concurrency()
        all_checks.extend(concurrency_checks)
        for check in concurrency_checks:
            mark = "PASS" if check.passed else "FAIL"
            detail = f"  - {check.detail}" if check.detail else ""
            print(f"   [{mark}] {check.name}{detail}")

        if args.audio:
            print("\naudio round trip")
            audio_checks = evaluate_audio(tmp)
            all_checks.extend(audio_checks)
            for check in audio_checks:
                mark = "PASS" if check.passed else "FAIL"
                detail = f"  - {check.detail}" if check.detail else ""
                print(f"   [{mark}] {check.name}{detail}")
        else:
            print("\n(skipping audio round trip; add --audio to include it)")

        passed = sum(1 for c in all_checks if c.passed)
        check_score = passed / len(all_checks) * 100 if all_checks else 0.0
        text_score = sum(scores) / len(scores) if scores else 0.0
        overall = (check_score + text_score) / 2

        print("\n" + "=" * 74)
        print(f"text fidelity (mean word-level F1) : {text_score:6.2f}%")
        print(f"checks passed                      : {check_score:6.2f}%  ({passed}/{len(all_checks)})")
        print(f"OVERALL ACCURACY                   : {overall:6.2f}%   (pass mark {PASS_MARK:.0f}%)")
        print("=" * 74)

        failures = [c for c in all_checks if not c.passed]
        if failures:
            print("\nfailed checks:")
            for check in failures:
                print(f"   {check.name}  {check.detail}")

        if overall >= PASS_MARK and not failures:
            print("\nPASS")
            return 0
        print("\nFAIL")
        return 1
    finally:
        if args.keep:
            print(f"\nfiles kept in: {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
