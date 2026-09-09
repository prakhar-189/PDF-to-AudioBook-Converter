"""Unit tests for `pdf_audiobook`.

WHAT THIS FOLDER IS
-------------------
`tests/` answers "is the code correct?". It is deliberately *not* the only
harness in this project, and the split matters:

    tests/      unit level. Fast, offline, and when one fails it names a
                function. Run with `pytest`.

    eval.py     end to end. Builds PDFs whose exact text is known, converts
                them, and scores the narration against the original word for
                word. When it fails it names a percentage, not a function.
                Run with `python eval.py`.

Neither replaces the other. A green `pytest` says every part behaves as
designed; `eval.py` says the parts together still extract 99%+ of the real
text. It is entirely possible to pass one and fail the other, which is the
reason both exist.

HOW THESE TESTS STAY FAST AND OFFLINE
-------------------------------------
Nothing here touches the network or the speech service. Two devices do that:

* **A stub engine.** `audiobook.get_engine` is monkeypatched to return an
  object that writes a deterministic byte string instead of calling Microsoft.
  The orchestration, resume logic and REST contract are all exercised for
  real; only the synthesis itself is faked.

* **Generated PDFs.** `conftest.py` builds the fixture documents with PyMuPDF
  at test time, complete with running headers, footers, page numbers and a
  word hyphenated across a line break. No binary fixtures are committed, and
  the expected text is known exactly because we wrote it.

WHAT IS COVERED WHERE
---------------------
    test_extract.py     text repair and chapter detection - the running-head
                        rules, de-hyphenation, reflow, and the page-range and
                        scanned-PDF error paths.
    test_tts.py         chunking (nothing lost, nothing split mid-sentence),
                        rate parsing, rate-limit detection, the adaptive
                        limiter, and audio merging.
    test_estimate.py    the wait-time arithmetic and the guards that stop it
                        calibrating on evidence it should ignore.
    test_audiobook.py   filenames, the resume manifest, and conversion end to
                        end against the stub engine.
    test_api.py         the REST contract: status codes, the job lifecycle,
                        and the upload guards.

Test methods are named as complete sentences, so the failure output reads as
a description of the broken behaviour rather than a symbol to go look up.
"""
