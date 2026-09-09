"""Drag-and-drop web UI.

    streamlit run app.py

Upload a PDF, press one button, download an MP3.

Anything that is not that decision lives in the sidebar under "Advanced",
collapsed. The main column is four controls and a button.

Two things matter for correctness here, because Streamlit re-runs this whole
file on every click: the upload is written once and atomically, and the parse
is cached. Without either, a big book is rewritten and re-extracted on every
keystroke - which corrupts the file mid-write and makes the page crawl.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import streamlit as st

from pdf_audiobook.audiobook import convert, is_finished, load_manifest
from pdf_audiobook.estimate import describe, observed_speedup
from pdf_audiobook.extract import load_pdf
from pdf_audiobook.tts import DEFAULT_CONCURRENCY, MAX_CONCURRENCY, EdgeEngine, OfflineEngine

OUTPUT_ROOT = Path("audiobooks")

# A short list in plain language. All 300+ voices are one checkbox away.
VOICES = [
    ("Aria - American, warm", "en-US-AriaNeural"),
    ("Guy - American, steady", "en-US-GuyNeural"),
    ("Christopher - American, deep", "en-US-ChristopherNeural"),
    ("Sonia - British, crisp", "en-GB-SoniaNeural"),
    ("Ryan - British, relaxed", "en-GB-RyanNeural"),
    ("Neerja - Indian English", "en-IN-NeerjaNeural"),
    ("Prabhat - Indian English, male", "en-IN-PrabhatNeural"),
    ("Natasha - Australian", "en-AU-NatashaNeural"),
]

SPEEDS = {"Slower": -20, "Normal": 0, "Faster": 20, "Fastest": 40}

st.set_page_config(page_title="PDF to Audiobook", page_icon="🎧", layout="centered")


@st.cache_data(show_spinner=False)
def all_edge_voices() -> list[str]:
    try:
        return [v["ShortName"] for v in EdgeEngine.list_voices()]
    except Exception:  # noqa: BLE001 - offline or blocked: fall back to the built-in list
        return [short for _, short in VOICES]


@st.cache_data(show_spinner=False)
def system_voices() -> list[str]:
    try:
        return [v["ShortName"] for v in OfflineEngine.list_voices()]
    except Exception:  # noqa: BLE001 - no OS speech engine installed: offer nothing
        return []


def save_upload(upload) -> Path:
    """Write the upload once, atomically.

    Streamlit re-runs this script on every interaction. Rewriting a 30 MB PDF
    each time is slow, and a re-run landing mid-write leaves a truncated file
    that MuPDF cannot parse. Write to a temporary name and rename into place -
    a rename is atomic, so a reader sees either the old file or the new one,
    never half of one.
    """
    folder = Path(tempfile.gettempdir()) / "pdf_audiobook_uploads"
    folder.mkdir(exist_ok=True)
    target = folder / upload.name

    data = upload.getbuffer()
    if target.exists() and target.stat().st_size == data.nbytes:
        return target  # already have this exact file

    scratch = folder / f".{upload.name}.part"
    scratch.write_bytes(data)
    os.replace(scratch, target)
    return target


@st.cache_data(show_spinner="Reading the PDF...")
def read_book(path: str, size: int, first_page: int, last_page: int | None,
              pages_per_part: int, use_toc: bool, toc_level: str, clean: bool):
    """Parse the PDF. Cached, so moving a slider does not re-read 5000 pages."""
    return load_pdf(path, first_page=first_page, last_page=last_page,
                    pages_per_part=pages_per_part, use_toc=use_toc,
                    toc_level=toc_level, clean=clean)


def pretty_size(path: Path) -> str:
    mb = path.stat().st_size / 1e6
    return f"{mb / 1000:.1f} GB" if mb >= 1000 else f"{mb:.0f} MB"


# --------------------------------------------------------------------------
# Advanced settings: sidebar, collapsed, and read before the main column
# needs them, since Streamlit runs this file top to bottom.
# --------------------------------------------------------------------------

with st.sidebar:
    st.header("Advanced")
    st.caption("You can ignore all of this.")

    with st.expander("Voice and audio"):
        engine = st.radio(
            "Voice engine", ["edge", "offline"],
            format_func=lambda e: ("Natural voices (needs internet)" if e == "edge"
                                   else "Offline voices (no internet)"),
        )
        every_voice = st.checkbox("Show all voices (90+ languages)", value=False)
        pitch = st.slider("Pitch", -30, 30, 0, step=5,
                          help="Leave at 0 unless the voice sounds off to you.")
        jobs = st.slider("Parallel requests", 1, MAX_CONCURRENCY,
                         DEFAULT_CONCURRENCY,
                         help="How many pieces are synthesised at once. Higher "
                              "is faster; if the service starts refusing, the "
                              "app slows itself down automatically.")

    with st.expander("How the book is split"):
        split_mode = st.radio("Split into", ["Chapters", "Fixed page chunks"])
        use_toc = split_mode == "Chapters"
        if use_toc:
            toc_level = st.selectbox("Outline depth", ["auto", "1", "2", "3", "4"])
            pages_per_part = 25
        else:
            toc_level = "auto"
            pages_per_part = st.slider("Pages per file", 1, 200, 25)

    with st.expander("Page range and cleanup"):
        first_page = st.number_input("Start at page", min_value=1, value=1, step=1)
        last_input = st.number_input("End at page (0 = last)", min_value=0, value=0, step=1)
        last_page = int(last_input) or None
        clean = st.checkbox("Remove headers, footers, page numbers", value=True)
        speak_titles = st.checkbox("Read chapter titles aloud", value=True)


# --------------------------------------------------------------------------
# The whole app, for someone who just wants an MP3.
# --------------------------------------------------------------------------

st.title("🎧 PDF to Audiobook")
st.caption("Upload a PDF. Get an MP3 you can listen to anywhere.")

uploaded = st.file_uploader("Choose a PDF", type="pdf")
if not uploaded:
    st.stop()

pdf_path = save_upload(uploaded)

try:
    book = read_book(str(pdf_path), pdf_path.stat().st_size, int(first_page),
                     last_page, int(pages_per_part), use_toc, toc_level, clean)
except ValueError as exc:
    st.error(str(exc))
    st.stop()
except Exception as exc:  # noqa: BLE001 - a damaged or unreadable PDF
    st.error(f"This PDF could not be read: {exc}")
    st.caption("If it is a scan of page images, run it through OCR first.")
    st.stop()

hours, minutes = divmod(round(book.est_minutes), 60)
st.success(f"**{book.title}** - {len(book.chapters)} chapters, "
           f"{hours}h {minutes}m of listening")

# --- 1. voice ---------------------------------------------------------------
if engine == "offline":
    options = system_voices()
    voice = st.selectbox("Voice", options) if options else None
    if not options:
        st.warning("No system voices found. Switch to natural voices in Advanced.")
elif every_voice:
    options = all_edge_voices()
    start = options.index("en-US-AriaNeural") if "en-US-AriaNeural" in options else 0
    voice = st.selectbox("Voice", options, index=start)
else:
    lookup = dict(VOICES)
    voice = lookup[st.selectbox("Voice", [label for label, _ in VOICES])]

# --- 2. speed ---------------------------------------------------------------
rate = SPEEDS[st.select_slider("Reading speed", list(SPEEDS), value="Normal")]

# --- 3. how much ------------------------------------------------------------
numbers = [c.index for c in book.chapters]
titles = {c.index: c.title for c in book.chapters}
picked = numbers

if len(numbers) > 1:
    scope = st.radio("How much of it?", ["The whole book", "Just a part"],
                     horizontal=True)
    if scope == "Just a part":
        low, high = st.select_slider(
            "From / to", options=numbers, value=(numbers[0], numbers[-1]),
            format_func=lambda i: f"{i}. {titles[i][:30]}",
        )
        picked = [n for n in numbers if low <= n <= high]

# --- 4. go ------------------------------------------------------------------
out_dir = OUTPUT_ROOT / pdf_path.stem
manifest = load_manifest(out_dir)
ext = "wav" if engine == "offline" else "mp3"
already = {c.index for c in book.chapters
           if is_finished(c, manifest, out_dir, ext, voice)}

todo = [c for c in book.chapters if c.index in set(picked) and c.index not in already]
pending_words = sum(c.word_count for c in todo)

go = st.button("Make my audiobook", type="primary", use_container_width=True,
               disabled=voice is None)
st.caption(describe(pending_words, rate, engine,
                    observed_speedup(manifest, engine, jobs), jobs))

if go:
    progress = st.progress(0.0, text="Starting...")
    status = st.empty()
    state = {"done": 0}
    total = len(todo) or 1

    def on_event(kind: str, payload: dict) -> None:
        if kind in ("chapter", "skip"):
            status.caption(f"Reading: {payload['chapter'].title}")
        elif kind == "chunk":
            within = payload["done"] / max(payload["total"], 1)
            progress.progress(min((state["done"] + within) / total, 1.0),
                              text=f"Chapter {state['done'] + 1} of {total}")
        if kind in ("chapter_done", "skip"):
            state["done"] += 1

    try:
        result = convert(
            pdf_path, out_dir,
            engine=engine, voice=voice, rate=f"{rate:+d}%", pitch=f"{pitch:+d}Hz",
            first_page=int(first_page), last_page=last_page,
            pages_per_part=int(pages_per_part), use_toc=use_toc,
            toc_level=toc_level, clean=clean, chapters=picked, jobs=jobs,
            single_file=True, speak_titles=speak_titles, on_event=on_event,
        )
    except Exception as exc:  # noqa: BLE001 - shown plainly, never as a stack trace
        progress.empty()
        status.empty()
        st.error(f"Something went wrong: {exc}")
        st.stop()

    progress.empty()
    status.empty()
    st.session_state["audiobook"] = str(result.single_file or "")
    st.session_state["book_title"] = result.book.title

# --- 5. the file ------------------------------------------------------------
saved = st.session_state.get("audiobook")
if saved and Path(saved).exists():
    audio = Path(saved)
    title = st.session_state.get("book_title", audio.stem)

    st.divider()
    st.subheader("Your audiobook is ready")
    st.audio(str(audio))
    st.download_button(
        f"Download {audio.suffix.lstrip('.').upper()}  ({pretty_size(audio)})",
        audio.read_bytes(),
        file_name=f"{title}{audio.suffix}",
        mime="audio/mpeg" if audio.suffix == ".mp3" else "audio/wav",
        type="primary", use_container_width=True,
    )
    st.caption(f"Also saved on this computer at: {audio}")
