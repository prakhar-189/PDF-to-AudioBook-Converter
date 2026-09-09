"""Command line front end:  python -m pdf_audiobook book.pdf"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .audiobook import convert, default_out_dir, is_finished, load_manifest
from .estimate import build_seconds, humanise, observed_speedup
from .extract import load_pdf
from .tts import DEFAULT_CONCURRENCY, TTSError, get_engine

RECOMMENDED = [
    ("en-US-AriaNeural", "US female, warm and even - the best all-round narrator"),
    ("en-US-GuyNeural", "US male, steady"),
    ("en-US-ChristopherNeural", "US male, deeper newsreader tone"),
    ("en-GB-SoniaNeural", "UK female, crisp"),
    ("en-GB-RyanNeural", "UK male"),
    ("en-IN-NeerjaNeural", "Indian English female"),
    ("en-IN-PrabhatNeural", "Indian English male"),
    ("en-AU-NatashaNeural", "Australian female"),
]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pdf-audiobook",
        description="Turn a PDF into an audiobook you can listen to anywhere.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python -m pdf_audiobook book.pdf\n"
            "  python -m pdf_audiobook book.pdf -o out --rate +20% --single-file\n"
            "  python -m pdf_audiobook book.pdf --voice en-GB-RyanNeural\n"
            "  python -m pdf_audiobook book.pdf --pages 10-40 --dry-run\n"
            "  python -m pdf_audiobook --list-voices en\n"
        ),
    )
    p.add_argument("pdf", nargs="?", help="path to the PDF")
    p.add_argument("-o", "--out", help="output folder (default: '<pdf name> (audiobook)')")

    voice = p.add_argument_group("voice")
    voice.add_argument("--engine", choices=["edge", "offline"], default="edge",
                       help="edge = neural voices, needs internet (default); "
                            "offline = your OS voices, no internet")
    voice.add_argument("--voice", default="en-US-AriaNeural", help="voice short name")
    voice.add_argument("--rate", default="+0%", help="speed, e.g. +25%% or -10%% (default +0%%)")
    voice.add_argument("--pitch", default="+0Hz", help="pitch shift, e.g. -5Hz")
    voice.add_argument("--volume", default="+0%", help="volume, e.g. +10%%")
    voice.add_argument("--list-voices", nargs="?", const="", metavar="LANG",
                       help="list available voices, optionally filtered ('en', 'en-IN', 'hi')")

    text = p.add_argument_group("what to read")
    text.add_argument("--pages", help="page range to read, e.g. 5-120 or 30-")
    text.add_argument("--pages-per-part", type=int, default=25,
                      help="pages per file when the PDF has no table of contents (default 25)")
    text.add_argument("--toc-level", default="auto", choices=["auto", "1", "2", "3", "4"],
                      help="how deep to read the PDF outline. 'auto' (default) goes deeper "
                           "when the top level would give huge files, as in an omnibus")
    text.add_argument("--no-toc", action="store_true",
                      help="ignore the PDF outline and split by page count instead")
    text.add_argument("--no-clean", action="store_true",
                      help="skip header/footer removal and paragraph repair")
    text.add_argument("--no-titles", action="store_true",
                      help="do not read the chapter title aloud before each chapter")
    text.add_argument("--chapters", metavar="SPEC",
                      help="which chapters to convert, using the numbers from --dry-run: "
                           "'10', '10-15', or '3,7,20-22'")
    text.add_argument("--password", help="password for an encrypted PDF")

    out = p.add_argument_group("output")
    out.add_argument("--next", nargs="?", type=int, const=1, metavar="N", dest="next_n",
                     help="convert only the next N chapters that are not done yet "
                          "(default 1) - the easy way to work through a long book")
    out.add_argument("--jobs", type=int, default=DEFAULT_CONCURRENCY, metavar="N",
                     help=f"how many chunks to synthesise at once "
                          f"(default {DEFAULT_CONCURRENCY}; 1 disables concurrency). "
                          f"Higher is faster until the voice service starts "
                          f"refusing, at which point it backs off on its own")
    out.add_argument("--single-file", action="store_true",
                     help="also merge every chapter into one long file")
    out.add_argument("--no-resume", action="store_true",
                     help="re-synthesise chapters that were already finished")
    out.add_argument("--dry-run", action="store_true",
                     help="show the chapter breakdown and estimated length, make no audio")
    return p


def parse_pages(spec: str | None) -> tuple[int, int | None]:
    if not spec:
        return 1, None
    spec = spec.strip()
    if "-" not in spec:
        page = int(spec)
        return page, page
    start, _, end = spec.partition("-")
    return (int(start) if start.strip() else 1,
            int(end) if end.strip() else None)


def parse_chapters(spec: str | None) -> set[int] | None:
    """'10', '10-15', '3,7,20-22' -> the set of chapter numbers meant."""
    if not spec:
        return None

    wanted: set[int] = set()
    for part in spec.replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            start, _, end = part.partition("-")
            first = int(start) if start else 0
            last = int(end) if end else 9999
            if last < first:
                raise ValueError(f"Backwards chapter range: {part!r}")
            wanted.update(range(first, last + 1))
        else:
            wanted.add(int(part))

    if not wanted:
        raise ValueError(f"Could not read any chapter numbers from {spec!r}")
    return wanted


def show_voices(engine: str, language: str) -> int:
    cls = get_engine(engine, voice="en-US-AriaNeural").__class__
    voices = cls.list_voices(language or None)
    if not voices:
        print(f"No voices matched {language!r}.")
        return 1

    for v in voices:
        gender = (v.get("Gender") or "")[:1]
        print(f"  {v['ShortName']:<34} {gender:<2} {v.get('FriendlyName', '')}")
    print(f"\n{len(voices)} voices.")
    if engine == "edge" and not language:
        print("\nGood starting points:")
        for name, note in RECOMMENDED:
            print(f"  {name:<28} {note}")
    return 0


def preview(args, first_page: int, last_page: int | None,
            wanted: set[int] | None = None) -> int:
    book = load_pdf(args.pdf, first_page=first_page, last_page=last_page,
                    pages_per_part=args.pages_per_part, use_toc=not args.no_toc,
                    toc_level=args.toc_level, clean=not args.no_clean,
                    password=args.password)

    out_dir = Path(args.out) if args.out else default_out_dir(Path(args.pdf))
    manifest = load_manifest(out_dir)
    ext = "wav" if args.engine == "offline" else "mp3"

    source = "PDF table of contents" if book.used_toc else f"{args.pages_per_part}-page parts"
    print(f"\n{book.title or Path(args.pdf).stem}")
    if book.author:
        print(f"by {book.author}")
    print(f"{book.page_count} pages | split by {source} | {len(book.chapters)} chapters\n")

    shown = [c for c in book.chapters if wanted is None or c.index in wanted]
    if not shown:
        print(f"  Nothing matched --chapters {args.chapters!r}.")
        return 1

    done = 0
    for ch in shown:
        finished = is_finished(ch, manifest, out_dir, ext, args.voice)
        done += finished
        mark = "[done]" if finished else "      "
        print(f"  {mark} {ch.index:>3}. {ch.title[:48]:<50} "
              f"{ch.word_count:>7,} words  ~{ch.est_minutes:>5.1f} min")

    words = sum(c.word_count for c in shown)
    hours, minutes = divmod(round(sum(c.est_minutes for c in shown)), 60)
    print(f"\n  {len(shown)} chapters, {words:,} words, about {hours}h {minutes}m of audio.")

    todo_words = sum(c.word_count for c in shown
                     if not is_finished(c, manifest, out_dir, ext, args.voice))
    rate_pct = float(str(args.rate).strip().rstrip("%") or 0)
    speedup = observed_speedup(manifest, args.engine, args.jobs)
    build = build_seconds(todo_words, rate_pct, args.engine, speedup, args.jobs)
    measured = " (from your own measured speed)" if speedup else ""
    if done:
        print(f"  {done} already converted, {len(shown) - done} to go.")
    if todo_words:
        print(f"  About {humanise(build)} to build what is left{measured}.")
    print("\nRun again without --dry-run to make the audio,")
    print("or add --next to do just the next chapter.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list_voices is not None:
        return show_voices(args.engine, args.list_voices)

    if not args.pdf:
        parser.error("give me a PDF (or use --list-voices)")

    first_page, last_page = parse_pages(args.pages)

    try:
        wanted = parse_chapters(args.chapters)
        if args.dry_run:
            return preview(args, first_page, last_page, wanted)
        return run(args, first_page, last_page, wanted)
    except (ValueError, FileNotFoundError, TTSError) as exc:
        print(f"\nError: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nStopped. Finished chapters are kept - rerun to pick up where you left off.")
        return 130


def run(args, first_page: int, last_page: int | None,
        wanted: set[int] | None = None) -> int:
    from tqdm import tqdm

    state: dict = {"bar": None, "n": 0}

    def on_event(kind: str, payload: dict) -> None:
        if kind == "book":
            book = payload["book"]
            hours, minutes = divmod(round(book.est_minutes), 60)
            print(f"\n{book.title or Path(args.pdf).stem}"
                  f"{' by ' + book.author if book.author else ''}")
            print(f"{len(book.chapters)} chapters | {book.word_count:,} words "
                  f"| about {hours}h {minutes}m of audio\n")

        elif kind == "skip":
            state["n"] += 1
            print(f"[{state['n']}] {payload['chapter'].title[:56]} - already done, skipping")

        elif kind == "chapter":
            state["n"] += 1
            ch = payload["chapter"]
            label = f"[{state['n']}/{payload['total']}] {ch.title[:40]}"
            state["bar"] = tqdm(total=1, desc=label, unit="chunk", leave=True)

        elif kind == "chunk":
            bar = state["bar"]
            if bar is not None:
                bar.total = payload["total"]
                bar.n = payload["done"]
                bar.refresh()

        elif kind == "chapter_done":
            if state["bar"] is not None:
                state["bar"].close()
                state["bar"] = None

    result = convert(
        args.pdf, args.out,
        engine=args.engine, voice=args.voice, rate=args.rate,
        pitch=args.pitch, volume=args.volume,
        first_page=first_page, last_page=last_page,
        pages_per_part=args.pages_per_part, use_toc=not args.no_toc,
        toc_level=args.toc_level, clean=not args.no_clean, password=args.password,
        chapters=wanted, limit=args.next_n, jobs=args.jobs,
        single_file=args.single_file, resume=not args.no_resume,
        speak_titles=not args.no_titles, on_event=on_event,
    )

    if not result.files:
        print("\nNothing to do - every selected chapter is already converted.")
    else:
        size_mb = sum(f.stat().st_size for f in result.files) / 1e6
        print(f"\nDone in {result.seconds / 60:.1f} min.")
        print(f"{len(result.files)} files, {size_mb:.1f} MB in: {result.out_dir}")

    progress = (f", {result.remaining} to go" if result.remaining
                else " - the whole book is done")
    print(f"{result.completed} of {result.total} chapters converted{progress}.")
    if result.single_file:
        print(f"Single file: {result.single_file.name}")
    if result.remaining:
        print("Run again with --next for the next one.")
    print("Open playlist.m3u to play what you have, in order.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
