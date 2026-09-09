"""Turn any PDF into an audiobook.

WHAT THIS FOLDER IS
-------------------
`pdf_audiobook/` is the library — everything that does actual work lives here,
and nothing in it knows or cares who is calling. That separation is the whole
point of the layout: the same conversion code is driven by three different
front ends without being written three times.

    pdf_audiobook/          <- this folder: the engine
    app.py                  <- a person clicking buttons (Streamlit)
    eval.py                 <- the accuracy harness
    tests/                  <- the unit tests

THE PIPELINE, IN ORDER
----------------------
A PDF becomes audio by passing through these modules in sequence. Each one is
usable on its own, which is why `load_pdf()` is worth importing even if you
never intend to make a sound.

    extract.py    PDF -> clean, speakable text.
                  Undoes the damage a PDF does to prose: strips running heads,
                  footers and page numbers, rejoins words hyphenated across a
                  line break, reflows hard-wrapped lines into paragraphs, and
                  reads the embedded outline to find chapters.
                  Gives you a `Book` of `Chapter` objects.

    tts.py        text -> audio bytes.
                  Two interchangeable engines (neural `edge`, offline
                  `pyttsx3`), sentence-aware chunking, and the back-pressure
                  behaviour that keeps a free shared service from refusing us.

    audiobook.py  the two above, tied together.
                  Owns the output folder, the resume manifest, the playlist,
                  and the decision about which chapters still need doing.
                  `convert()` is the one function most callers want.

    estimate.py   "how long will this take?"
                  Pure arithmetic over word counts, with no side effects. It
                  calibrates itself from timings that `audiobook.py` records,
                  so the answer improves as you use the tool.

    cli.py        the command line.
    api.py        the REST service (optional: needs the `api` extra).

WHY THE SPLIT LOOKS LIKE THIS
-----------------------------
The dependency arrows only ever point one way — `cli` and `api` depend on
`audiobook`, which depends on `extract` and `tts`, which depend on nothing of
ours. No module imports a module that imports it back. That is what makes the
pieces testable in isolation and what lets `extract.py` be useful to anyone who
wants clean text out of a PDF for some entirely different purpose.
"""

# Bumped by hand. Reported by `--version`, by the API's /healthz, and used as
# the OpenAPI schema version.
__version__ = "1.0.0"
