"""A REST service around the converter.

The CLI and the Streamlit app both assume a person is sitting there waiting.
This is the third caller: another program. It matters because synthesis is
slow — a full book is minutes to hours — so the one thing the API must not do
is block a request until the audio is ready.

So conversion is a *job*: `POST /jobs` accepts the PDF, returns `202` with an
id, and the work happens on a background worker. Everything else is polling
that id. `POST /estimate` is the exception — it only reads the PDF, never
synthesises, so it can answer inline.

Run it with::

    pip install -e ".[api]"
    uvicorn pdf_audiobook.api:app --reload

Metrics for Prometheus are on ``/metrics``.
"""

from __future__ import annotations

import shutil
import tempfile
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from pydantic import BaseModel, Field

from . import __version__
from .audiobook import convert
from .estimate import audio_seconds, build_seconds, describe
from .extract import load_pdf
from .tts import DEFAULT_CONCURRENCY, MAX_CONCURRENCY, get_engine

# How many conversions run at once. Synthesis is network-bound rather than
# CPU-bound, but each job already opens `--jobs` connections of its own, so
# running many books in parallel is the fastest way to get rate-limited.
MAX_WORKERS = 2

# Uploads are held on disk for the life of the job. A cap keeps a careless
# caller from filling the disk.
MAX_UPLOAD_BYTES = 200 * 1024 * 1024  # 200 MB

# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------

JOBS_TOTAL = Counter(
    "pdf_audiobook_jobs_total", "Conversion jobs by terminal state.", ["status"]
)
JOB_SECONDS = Histogram(
    "pdf_audiobook_job_duration_seconds",
    "Wall-clock time of a completed conversion job.",
    buckets=(1, 5, 15, 60, 300, 900, 3600, 14400),
)
CHAPTERS_DONE = Counter(
    "pdf_audiobook_chapters_converted_total", "Chapters written as audio."
)
WORDS_DONE = Counter(
    "pdf_audiobook_words_total", "Words sent to the speech engine."
)
JOBS_RUNNING = Gauge(
    "pdf_audiobook_jobs_in_progress", "Jobs currently converting."
)

# ---------------------------------------------------------------------------
# job registry
# ---------------------------------------------------------------------------


class JobStatus(str, Enum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"


@dataclass
class Job:
    id: str
    filename: str
    status: JobStatus = JobStatus.queued
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None

    # progress
    chapters_total: int = 0
    chapters_done: int = 0
    current_chapter: Optional[str] = None

    # results
    words: int = 0
    error: Optional[str] = None
    workdir: Optional[Path] = None
    archive: Optional[Path] = None

    @property
    def progress(self) -> float:
        if not self.chapters_total:
            return 0.0
        return round(self.chapters_done / self.chapters_total, 4)


class JobRegistry:
    """In-memory job store.

    Deliberately in-memory: this service is a single process and jobs hold
    files in its own temp directory, so a restart invalidates them anyway.
    Running more than one replica means moving this to Redis and the workers
    to a real queue — see the note in the README.
    """

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def add(self, job: Job) -> None:
        with self._lock:
            self._jobs[job.id] = job

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def all(self) -> list[Job]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)

    def remove(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.pop(job_id, None)


registry = JobRegistry()
executor = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="convert")


# ---------------------------------------------------------------------------
# schemas
# ---------------------------------------------------------------------------


class ChapterOut(BaseModel):
    index: int
    title: str
    words: int
    minutes: float


class EstimateOut(BaseModel):
    title: str
    author: str
    pages: int
    used_toc: bool
    words: int
    chapters: list[ChapterOut]
    listening_seconds: float
    build_seconds: float
    summary: str


class JobOut(BaseModel):
    id: str
    filename: str
    status: JobStatus
    progress: float = Field(ge=0.0, le=1.0)
    chapters_done: int
    chapters_total: int
    current_chapter: Optional[str] = None
    words: int
    error: Optional[str] = None
    seconds: Optional[float] = None


class HealthOut(BaseModel):
    status: str
    version: str
    jobs_running: int


def _as_out(job: Job) -> JobOut:
    seconds = None
    if job.started_at:
        seconds = round((job.finished_at or time.time()) - job.started_at, 2)
    return JobOut(
        id=job.id,
        filename=job.filename,
        status=job.status,
        progress=job.progress,
        chapters_done=job.chapters_done,
        chapters_total=job.chapters_total,
        current_chapter=job.current_chapter,
        words=job.words,
        error=job.error,
        seconds=seconds,
    )


# ---------------------------------------------------------------------------
# app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="PDF → Audiobook",
    version=__version__,
    summary="Turn a PDF into chapter-split audiobook narration.",
)


@app.get("/healthz", response_model=HealthOut, tags=["ops"])
def healthz() -> HealthOut:
    running = sum(1 for j in registry.all() if j.status is JobStatus.running)
    return HealthOut(status="ok", version=__version__, jobs_running=running)


@app.get("/metrics", include_in_schema=False, tags=["ops"])
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/voices", tags=["voices"])
def voices(language: Optional[str] = None, engine: str = "edge") -> list[dict]:
    """Available narrators, optionally filtered by language prefix (``en-IN``)."""
    try:
        return get_engine(engine).list_voices(language)
    except ValueError as exc:  # unknown engine name
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # the voice list is a network call for `edge`
        raise HTTPException(
            status_code=503, detail=f"Could not reach the voice service: {exc}"
        ) from exc


def _save_upload(upload: UploadFile, into: Path) -> Path:
    """Stream an upload to disk, refusing anything oversized or not a PDF."""
    name = Path(upload.filename or "book.pdf").name
    if not name.lower().endswith(".pdf"):
        raise HTTPException(status_code=415, detail="Only PDF uploads are accepted.")

    target = into / name
    size = 0
    with open(target, "wb") as handle:
        while chunk := upload.file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"PDF is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.",
                )
            handle.write(chunk)
    if not size:
        raise HTTPException(status_code=400, detail="The uploaded file is empty.")
    return target


@app.post("/estimate", response_model=EstimateOut, tags=["convert"])
def estimate(
    file: UploadFile = File(..., description="The PDF to inspect."),
    rate: str = Form("+0%"),
    engine: str = Form("edge"),
    jobs: int = Form(DEFAULT_CONCURRENCY),
    first_page: int = Form(1),
    last_page: Optional[int] = Form(None),
    use_toc: bool = Form(True),
) -> EstimateOut:
    """Chapter breakdown and timings, without synthesising a single word.

    This is the call to make before `POST /jobs`: it is the difference between
    committing to a five-hour conversion and finding out afterwards that the
    page range was wrong.
    """
    with tempfile.TemporaryDirectory() as tmp:
        pdf = _save_upload(file, Path(tmp))
        try:
            book = load_pdf(
                pdf,
                first_page=first_page,
                last_page=last_page,
                use_toc=use_toc,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        rate_pct = float(str(rate).strip().rstrip("%") or 0)
    except ValueError:
        rate_pct = 0.0

    return EstimateOut(
        title=book.title or pdf.stem,
        author=book.author,
        pages=book.page_count,
        used_toc=book.used_toc,
        words=book.word_count,
        chapters=[
            ChapterOut(index=c.index, title=c.title, words=c.word_count,
                       minutes=round(c.est_minutes, 2))
            for c in book.chapters
        ],
        listening_seconds=round(audio_seconds(book.word_count, rate_pct), 1),
        build_seconds=round(
            build_seconds(book.word_count, rate_pct, engine, jobs=jobs), 1
        ),
        summary=describe(book.word_count, rate_pct, engine, jobs=jobs),
    )


def _run_job(job: Job, pdf: Path, options: dict) -> None:
    """Worker body. Runs off the request thread; must never raise."""
    job.status = JobStatus.running
    job.started_at = time.time()
    JOBS_RUNNING.inc()

    def on_event(kind: str, payload: dict) -> None:
        if kind == "book":
            job.chapters_total = len(payload.get("selected") or [])
        elif kind == "chapter":
            chapter = payload.get("chapter")
            job.current_chapter = getattr(chapter, "title", None)
        elif kind in ("chapter_done", "skip"):
            job.chapters_done += 1
            CHAPTERS_DONE.inc()

    try:
        result = convert(pdf, out_dir=job.workdir, on_event=on_event, **options)
        job.words = result.book.word_count
        WORDS_DONE.inc(result.book.word_count)

        # Zip on the worker, not on the download request: a 165-hour boxed set
        # is gigabytes, and compressing it inside a GET would hold the
        # connection open long past any sensible client timeout.
        archive = Path(tempfile.gettempdir()) / f"pdf-audiobook-{job.id}.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_STORED) as zf:
            for path in sorted(result.out_dir.rglob("*")):
                if path.is_file() and not path.name.startswith("."):
                    zf.write(path, path.relative_to(result.out_dir))
        job.archive = archive
        job.status = JobStatus.succeeded
        JOBS_TOTAL.labels(status="succeeded").inc()

    except Exception as exc:  # noqa: BLE001 - surfaced to the caller as job state
        job.status = JobStatus.failed
        job.error = f"{type(exc).__name__}: {exc}"
        JOBS_TOTAL.labels(status="failed").inc()

    finally:
        job.finished_at = time.time()
        JOBS_RUNNING.dec()
        JOB_SECONDS.observe(job.finished_at - (job.started_at or job.finished_at))
        job.current_chapter = None


@app.post("/jobs", response_model=JobOut, status_code=202, tags=["convert"])
def create_job(
    file: UploadFile = File(..., description="The PDF to convert."),
    engine: str = Form("edge"),
    voice: str = Form("en-US-AriaNeural"),
    rate: str = Form("+0%"),
    first_page: int = Form(1),
    last_page: Optional[int] = Form(None),
    use_toc: bool = Form(True),
    pages_per_part: int = Form(25),
    single_file: bool = Form(False),
    jobs: int = Form(DEFAULT_CONCURRENCY),
    limit: Optional[int] = Form(None),
) -> JobOut:
    """Queue a conversion. Returns immediately with an id to poll."""
    if engine not in ("edge", "offline"):
        raise HTTPException(status_code=400, detail=f"Unknown engine {engine!r}.")
    if not 1 <= jobs <= MAX_CONCURRENCY:
        raise HTTPException(
            status_code=400, detail=f"jobs must be between 1 and {MAX_CONCURRENCY}."
        )

    workdir = Path(tempfile.mkdtemp(prefix="pdf-audiobook-"))
    try:
        pdf = _save_upload(file, workdir)
    except HTTPException:
        shutil.rmtree(workdir, ignore_errors=True)
        raise

    job = Job(id=uuid.uuid4().hex[:12], filename=pdf.name, workdir=workdir / "out")
    registry.add(job)

    executor.submit(
        _run_job, job, pdf,
        {
            "engine": engine, "voice": voice, "rate": rate,
            "first_page": first_page, "last_page": last_page,
            "use_toc": use_toc, "pages_per_part": pages_per_part,
            "single_file": single_file, "jobs": jobs, "limit": limit,
        },
    )
    return _as_out(job)


@app.get("/jobs", response_model=list[JobOut], tags=["convert"])
def list_jobs() -> list[JobOut]:
    return [_as_out(j) for j in registry.all()]


@app.get("/jobs/{job_id}", response_model=JobOut, tags=["convert"])
def get_job(job_id: str) -> JobOut:
    job = registry.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No such job.")
    return _as_out(job)


@app.get("/jobs/{job_id}/download", tags=["convert"])
def download(job_id: str) -> FileResponse:
    """The finished audiobook as a zip: one MP3 per chapter plus the playlist."""
    job = registry.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No such job.")
    if job.status is JobStatus.failed:
        raise HTTPException(status_code=409, detail=job.error or "Conversion failed.")
    if job.status is not JobStatus.succeeded or not job.archive:
        # 409, not 404: the id is real, the audio just is not ready yet.
        raise HTTPException(status_code=409, detail=f"Job is {job.status.value}.")

    stem = Path(job.filename).stem
    return FileResponse(job.archive, media_type="application/zip",
                        filename=f"{stem} (audiobook).zip")


@app.delete("/jobs/{job_id}", status_code=204, tags=["convert"])
def delete_job(job_id: str, background: BackgroundTasks) -> Response:
    """Forget a job and delete its audio."""
    job = registry.remove(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No such job.")

    # After the response, so the caller is not made to wait on disk I/O.
    if job.workdir:
        background.add_task(shutil.rmtree, job.workdir.parent, ignore_errors=True)
    if job.archive:
        background.add_task(job.archive.unlink, missing_ok=True)
    return Response(status_code=204)
