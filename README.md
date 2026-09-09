# 🎧 PDF → Audiobook

[![ci](https://github.com/prakhar-189/PDF-to-AudioBook-Converter/actions/workflows/ci.yml/badge.svg)](https://github.com/prakhar-189/PDF-to-AudioBook-Converter/actions/workflows/ci.yml)
[![eval](https://github.com/prakhar-189/PDF-to-AudioBook-Converter/actions/workflows/eval.yml/badge.svg)](https://github.com/prakhar-189/PDF-to-AudioBook-Converter/actions/workflows/eval.yml)
[![python](https://img.shields.io/badge/python-3.9%20%E2%80%93%203.12-3670A0?logo=python&logoColor=ffdd54)](https://www.python.org/)
[![licence](https://img.shields.io/badge/licence-MIT-green)](LICENSE)

Drop in a PDF, get an audiobook. Chapter-by-chapter audio files, a playlist, and
a voice that actually sounds like a person.

Built for people who would rather listen than read.

Three ways in — a command line, a drag-and-drop page, and a REST service — all
driving the same converter. Extraction accuracy is measured at **99.7%** and
enforced as a CI gate, and every number in this README is one the repository
measures rather than one it claims.

---

## Quick start

```bash
pip install -r requirements.txt
python -m pdf_audiobook "some book.pdf"
```

That's it. You get a folder next to the PDF called `some book (audiobook)`
containing one MP3 per chapter plus a `playlist.m3u` you can open in VLC, on
your phone, or in any music player.

Prefer clicking to typing?

```bash
streamlit run app.py
```

…which opens a page where you drop the PDF in, pick a voice, press one button,
and download a single MP3.

And if the caller is another program rather than a person, there is a REST
service — see [An API for other programs](#an-api-for-other-programs).

---

## What it actually does

Feeding raw PDF text to a speech engine sounds terrible, because a PDF is a
description of ink on a page, not a document. Half the work here is repair:

| Problem in the PDF | What you'd hear without the fix | Handled by |
|---|---|---|
| Every page repeats a header and footer | "A Test Book. Chapter Two." between every page | repeated-line detection |
| Page numbers floating in the text | "…the harbour. Forty-seven. Sailors moved…" | page-number filter |
| Words split across lines: `any-\nthing` | "any… thing" | de-hyphenation |
| Lines hard-wrapped at the page width | Unnatural pauses mid-sentence | paragraph reflow |
| Citations like `[12]` | "bracket twelve" | citation stripping |
| Chapters | One eight-hour file you can't navigate | the PDF's own table of contents |

If the PDF has an embedded table of contents, that becomes your chapter list —
titles and all. If it doesn't, the book is split into fixed-size parts so no
single file is unmanageable.

**Outline depth is chosen for you.** In an omnibus or a boxed set, the top level
of the contents is the *books*, not the chapters — splitting there would hand
you five twenty-seven-hour files. When the top level would produce parts that
big and a deeper level exists, it goes deeper automatically. Override with
`--toc-level 1` (or 2, 3, 4) if you disagree with the choice.

---

## Voices

Two engines:

**`edge`** (default) — Microsoft's neural voices. Free, no API key, no account,
but needs an internet connection. These are the ones worth listening to for
hours.

**`offline`** — the voices already on your machine. Works on a plane, sounds
like a satnav.

```bash
python -m pdf_audiobook --list-voices          # everything
python -m pdf_audiobook --list-voices en-IN    # just Indian English
python -m pdf_audiobook --list-voices hi       # Hindi
```

Good starting points: `en-US-AriaNeural` (default, warm and even),
`en-US-ChristopherNeural` (deeper), `en-GB-RyanNeural`, `en-IN-NeerjaNeural`.

Over 300 voices across ~90 languages are available — the tool will happily read
a Hindi or Spanish PDF if you pick a matching voice.

---

## Useful flags

```bash
# Look before you leap: chapter breakdown and total listening time, no audio
python -m pdf_audiobook book.pdf --dry-run

# Faster narration (most people settle around +20%)
python -m pdf_audiobook book.pdf --rate +20%

# A different voice
python -m pdf_audiobook book.pdf --voice en-GB-RyanNeural

# Skip the index and the 30 pages of front matter
python -m pdf_audiobook book.pdf --pages 12-340

# One long file as well as the chapters
python -m pdf_audiobook book.pdf --single-file

# No internet
python -m pdf_audiobook book.pdf --engine offline

# More requests in parallel (default 4, use 1 to disable)
python -m pdf_audiobook book.pdf --jobs 8
```

Run `python -m pdf_audiobook --help` for the rest.

---

## The web UI

```bash
streamlit run app.py
```

Four controls and a button:

1. **Choose a PDF**
2. **Voice** — eight narrators in plain language ("Aria - American, warm").
   Tick *Show all voices* in Advanced for the full 300+ across 90 languages.
3. **Reading speed** — Slower / Normal / Faster / Fastest
4. **How much of it?** — the whole book, or drag a from/to slider over chapters

Press **Make my audiobook**, and you get one MP3 with a Download button, plus a
player to listen in the browser. The line under the button tells you the wait
before you commit to it.

Everything else — engine, pitch, split mode, page range, cleanup toggles — is
in the sidebar under **Advanced**, collapsed. Nothing was removed.

---

## Two ways to split a book

**By chapter** (default) — follows the PDF's own contents page, so files get
real titles like `14 - Chapter 8.mp3`. Depth is chosen automatically.

**By page count** — evenly sized files regardless of what the PDF looks like,
and the only option for a PDF with no contents page at all:

```bash
python -m pdf_audiobook book.pdf --no-toc --pages-per-part 50
```

In the web UI both live under **Advanced → How the book is split**, with a
slider for pages-per-file when you choose page chunks. Picking one never takes
the other away.

---

## Talking to the voice service

Synthesis is the slow part and it is somebody else's server, so it is treated
like one:

- **Bounded concurrency.** Four requests in flight by default (`--jobs`),
  never unbounded. Chunks are written back in the order the text was written,
  not the order they happen to finish — MP3 frames are order-sensitive, so
  reassembly is windowed rather than append-as-you-go.
- **Adaptive back-off.** A `429` or a throttling message permanently retires
  one worker for the rest of the run. If the service says we are asking for
  too much, going back to the same rate would just earn another refusal.
- **Jittered exponential retry**, so workers that fail together do not all
  return at the same instant and fail together again.
- **Empty responses are treated as failures.** A successful response carrying
  no audio raises nothing at all; unchecked it writes a zero-byte file that
  looks exactly like a finished chapter.

`eval.py` covers this without touching the network: the assembly path is
driven with a stand-in synthesiser whose later chunks finish *first*, so an
ordering bug is guaranteed to surface rather than depending on timing luck.

---

## How long will it take?

Every screen that offers to build something tells you the wait first. Under the
Convert button in the web UI, and at the bottom of `--dry-run`:

```
Approximately 13 minutes to make the audio file - 3h 9m of listening, 34,078 words
```

The line updates live as you change the split, the page range, the speed, or
which chapters you picked.

The first estimate uses a measured default (about 12× real time on the neural
engine). After that it uses **your own** throughput: each conversion records how
long it actually took, and later estimates say *"from your own measured speed"*.
On this machine the measured figure came out at 14.9×, so estimates dropped by
about a third once there was real data to go on.

---

## Checking it works

Two harnesses, answering two different questions.

**Is the code correct?** — `pytest`, over the pieces:

```bash
pip install -e ".[api,dev]"
pytest
```

135 tests covering text repair, chunking, the adaptive rate-limit behaviour,
the resume manifest, and the REST contract. Conversion is driven with a stub
engine, so the suite runs offline in a couple of seconds. When one fails it
names a function, not a percentage.

**Is the extraction still accurate?** — `eval.py`, end to end:

```bash
python eval.py              # text and structure, fast, no network
python eval.py --audio      # also builds real audio and checks it
```

The pass mark is 95%. The test PDFs are generated by `eval.py` itself, so the
exact body text that went in is known, and the narration that comes out is
compared against it word for word. Each document deliberately includes
everything the extractor has to survive: running headers, footers, page
numbers, words hyphenated across line breaks, citation markers and
hard-wrapped lines.

It reports **recall** (how much of the real text survived), **precision** (how
much of the narration is real text rather than page furniture), whether every
piece of page furniture was removed, whether chapters were detected, and — with
`--audio` — whether the finished recording is long enough to actually contain
all those words, which is how a silently dropped chunk gets caught.

Current score:

```
text fidelity (mean word-level F1) :  99.43%
checks passed                      : 100.00%  (30/30)
OVERALL ACCURACY                   :  99.71%   (pass mark 95%)
```

This is not decoration. Its first run failed at 92.59% and exposed a real bug:
body sentences that happened to sit at a page edge on several pages were being
deleted as if they were running headers. Header detection now requires three
signals to agree — the text sits in the page margin, it reads like a label
rather than prose, and it recurs — which took recall on the worst case from
81.4% to 100%.

---

## One chapter at a time

A 165-hour boxed set is not something you convert in one sitting, and you do
not need to. Work through it a chapter at a time:

```bash
# What is in the book, and what have I already done?
python -m pdf_audiobook book.pdf --dry-run

# Convert just the next chapter that is not done yet
python -m pdf_audiobook book.pdf --next

# Or the next five
python -m pdf_audiobook book.pdf --next 5

# Or exactly the ones you want, by the numbers from --dry-run
python -m pdf_audiobook book.pdf --chapters 14
python -m pdf_audiobook book.pdf --chapters 14-20
python -m pdf_audiobook book.pdf --chapters 3,7,20-22
```

`--dry-run` marks the chapters already converted with `[done]`, and every run
finishes by telling you where you stand:

```
4 of 384 chapters converted, 380 to go.
Run again with --next for the next one.
```

`playlist.m3u` is rewritten after every run and always covers everything
finished so far, in chapter order — so you can start listening after the first
chapter and keep converting as you go.

A file is only counted as done if it still matches the current text and voice.
Change the voice or the page range and the affected chapters quietly go back
into the queue rather than leaving you with a book narrated by two people.

---

## An API for other programs

The CLI and the web UI both assume a person is waiting. This is the third
caller.

```bash
pip install -e ".[api]"
uvicorn pdf_audiobook.api:app --reload
```

Interactive docs are then on <http://localhost:8000/docs>.

Synthesis takes minutes to hours, so conversion is a **job** rather than a
request that blocks:

```bash
# What am I in for? Reads the PDF, synthesises nothing, answers immediately.
curl -F file=@book.pdf http://localhost:8000/estimate

# Start a conversion - returns 202 and an id straight away
curl -F file=@book.pdf -F voice=en-GB-RyanNeural http://localhost:8000/jobs

# Poll it
curl http://localhost:8000/jobs/a1b2c3d4e5f6

# Collect the audio: a zip of one MP3 per chapter, plus the playlist
curl -OJ http://localhost:8000/jobs/a1b2c3d4e5f6/download
```

| Route | What it does |
|---|---|
| `POST /estimate` | Chapter breakdown and timings. Never synthesises. |
| `POST /jobs` | Queue a conversion. `202` with a job id. |
| `GET /jobs` · `GET /jobs/{id}` | Progress, chapter counts, errors. |
| `GET /jobs/{id}/download` | The finished audiobook as a zip. |
| `DELETE /jobs/{id}` | Forget the job and delete its audio. |
| `GET /voices` | The 300+ narrators, filterable by language. |
| `GET /healthz` · `GET /metrics` | Liveness, and Prometheus counters. |

Asking for the audio before it exists returns **409, not 404** — the id is
real, the work just is not finished, and a 404 would tell the caller to stop
polling.

`/metrics` exposes jobs by terminal state, a job-duration histogram, chapters
and words converted, and a gauge of jobs in flight — enough to see throughput
and failure rate on a dashboard without reading the logs.

**One process on purpose.** Jobs live in this process's memory and its temp
directory, so a second worker would 404 on jobs it has never heard of.
Scaling past one replica means moving the registry to Redis and the workers to
a real queue; the `JobRegistry` class is the seam where that swap happens.

---

## Running it in Docker

```bash
docker compose up api     # REST service on http://localhost:8000/docs
docker compose up ui      # drag-and-drop page on http://localhost:8501
```

One image serves both; they differ only in the command. It is a two-stage
build, so the compiler toolchain and the pip cache never reach the runtime
image, and the service runs as a non-root user because nothing it does needs
privileges.

`espeak-ng` is installed in the image, which is what makes `engine=offline`
work inside the container — a conversion with no internet connection at all.

---

## Things worth knowing

**It resumes.** Interrupt a 600-page book with Ctrl+C and rerun it — finished
chapters are kept and skipped. Change the voice or the page range and it
correctly redoes the work, because the manifest tracks both.

**No ffmpeg required.** Chapters are written straight from the speech engine,
and `--single-file` merges them at the frame level. Nothing to install.

**Scanned PDFs won't work.** If your PDF is photographs of pages, there is no
text to extract and you'll get a clear error. Run it through OCR first:

```bash
pip install ocrmypdf
ocrmypdf scanned.pdf searchable.pdf
```

**Check the text before committing to a long conversion.** `--dry-run` prints
the chapter list and total hours. Academic PDFs with two-column layouts,
heavy footnotes or lots of tables are the ones most likely to need
`--pages` to skip the awkward parts.

**Speed.** Chunks are synthesised in parallel, four at a time by default.
Measured on the same 24-minute chapter:

| | wall clock | throughput |
|---|---|---|
| `--jobs 1` (sequential) | 125 s | 11.6× real time |
| `--jobs 4` (default) | **48 s** | **30× real time** |

So a 10-hour book takes roughly 20 minutes, and a 165-hour boxed set about
five and a half hours rather than most of a day.

The two files are byte-identical in length — same duration, same 60,514 MPEG
frames — so the speed costs nothing in fidelity. Raise it with `--jobs 8` if
your connection is quick; the pool shrinks itself if the service starts
refusing requests.

---

## Layout

```
pdf_audiobook/          the library - everything that does real work
  __init__.py           what the package is, and how the modules fit together
  extract.py            PDF → clean, speakable text; chapter detection
  tts.py                the two speech engines, chunking, merging
  audiobook.py          orchestration, resume, playlist
  estimate.py           how long will this take, and self-calibration
  cli.py                the command line
  api.py                the REST service (FastAPI, optional extra)
  __main__.py           entry point for `python -m pdf_audiobook`

app.py                  drag-and-drop web UI (Streamlit)
eval.py                 end-to-end accuracy harness, and the CI gate

tests/                  unit tests (pytest)
  __init__.py           what this folder covers, and why eval.py is separate
  conftest.py           generated PDF and WAV fixtures

pyproject.toml          packaging, ruff and pytest configuration
Dockerfile              multi-stage image, runs as a non-root user
docker-compose.yml      the API and the UI from that one image
.github/workflows/      ci.yml (lint, tests, container) · eval.yml (accuracy)
```

The dependency arrows only ever point one way: `cli` and `api` depend on
`audiobook`, which depends on `extract` and `tts`, which depend on nothing of
ours. Nothing imports a module that imports it back.

That is what makes the pieces independently testable, and it is why
`load_pdf()` is worth importing on its own — it gives you a `Book` of `Chapter`
objects with clean text, which is useful whenever you want readable prose out
of a PDF and have no interest in audio at all.

**Where to read next.** Every module, class and function carries a docstring
explaining *why* it is the way it is, not just what it does. The two
`__init__.py` files are the intended starting points: `pdf_audiobook/__init__.py`
maps the pipeline end to end, and `tests/__init__.py` explains how the two test
harnesses differ and why both exist.

---

## Licence

This project is released under the **MIT Licence** — see [LICENSE](LICENSE).
Copyright (c) 2026 Prakhar Srivastava.

That covers *this code*, and nothing else. The books you feed it are a separate
matter: converting something you own for your own listening is fine,
distributing audio generated from a copyrighted book is not. An MIT licence on
the converter grants you no rights over the material you convert with it.
