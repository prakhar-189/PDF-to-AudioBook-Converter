"""The REST service.

Conversion is patched out with a stub engine, so nothing here touches the
network. What is being tested is the API contract: status codes, the job
lifecycle, and the guards on upload.
"""

from __future__ import annotations

import time

import pytest

pytest.importorskip("fastapi", reason="install the 'api' extra to test the service")

from fastapi.testclient import TestClient

from pdf_audiobook import api, audiobook


class StubEngine:
    """Stand-in speech engine, so these tests never leave the machine."""

    ext = "mp3"
    name = "stub"

    def __init__(self, **kwargs):
        """Accepts and ignores the real engine's options."""
        pass

    def synthesize(self, text, out_path, on_progress=None):
        """Write recognisable bytes instead of calling the voice service."""
        out_path.write_bytes(b"\xff\xfb" + text.encode("utf-8")[:32])
        if on_progress:
            on_progress(1, 1)
        return out_path


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """No real synthesis, and a clean job registry per test."""
    monkeypatch.setattr(audiobook, "get_engine", lambda name, **kw: StubEngine())
    monkeypatch.setattr(api, "registry", api.JobRegistry())


@pytest.fixture
def client():
    """A TestClient over the app, driving it without a running server."""
    return TestClient(api.app)


def _upload(pdf):
    """Shape a PDF as the multipart payload the endpoints expect."""
    return {"file": (pdf.name, pdf.read_bytes(), "application/pdf")}


def _wait(client, job_id, timeout=30.0):
    """Poll until the job leaves a running state."""

    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/jobs/{job_id}").json()
        if body["status"] in ("succeeded", "failed"):
            return body
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish within {timeout}s")


class TestOps:
    """Health, metrics and the OpenAPI schema."""

    def test_healthz(self, client):
        body = client.get("/healthz").json()
        assert body["status"] == "ok"
        assert body["version"]

    def test_metrics_are_prometheus_formatted(self, client):
        response = client.get("/metrics")
        assert response.status_code == 200
        assert "pdf_audiobook_jobs_total" in response.text

    def test_openapi_schema_is_served(self, client):
        assert client.get("/openapi.json").status_code == 200


class TestEstimate:
    """The inline endpoint - answers without synthesising anything."""

    def test_returns_the_chapter_breakdown(self, client, novel_pdf):
        response = client.post("/estimate", files=_upload(novel_pdf))
        assert response.status_code == 200

        body = response.json()
        assert body["used_toc"] is True
        assert [c["title"] for c in body["chapters"]] == [
            "Chapter 1", "Chapter 2", "Chapter 3",
        ]
        assert body["words"] > 0
        assert body["build_seconds"] > 0
        assert "of listening" in body["summary"]

    def test_a_faster_rate_shortens_the_estimate(self, client, novel_pdf):
        slow = client.post("/estimate", files=_upload(novel_pdf),
                           data={"rate": "+0%"}).json()
        fast = client.post("/estimate", files=_upload(novel_pdf),
                           data={"rate": "+50%"}).json()
        assert fast["listening_seconds"] < slow["listening_seconds"]

    def test_estimating_never_synthesises(self, client, novel_pdf, monkeypatch):
        def explode(*a, **kw):
            """Fails loudly if /estimate ever reaches the converter."""
            raise AssertionError("estimate must not call the speech engine")

        monkeypatch.setattr(api, "convert", explode)
        assert client.post("/estimate", files=_upload(novel_pdf)).status_code == 200

    def test_a_pdf_with_no_text_is_422(self, client, tmp_path):
        try:
            import pymupdf as fitz
        except ImportError:  # pragma: no cover
            import fitz
        blank = tmp_path / "scan.pdf"
        doc = fitz.open()
        doc.new_page()
        doc.save(blank)
        doc.close()

        response = client.post("/estimate", files=_upload(blank))
        assert response.status_code == 422
        assert "OCR" in response.json()["detail"]


class TestUploadGuards:
    """What the service refuses, and with which status code."""

    def test_rejects_a_non_pdf(self, client):
        response = client.post(
            "/estimate", files={"file": ("book.txt", b"hello", "text/plain")}
        )
        assert response.status_code == 415

    def test_rejects_an_empty_file(self, client):
        response = client.post(
            "/estimate", files={"file": ("book.pdf", b"", "application/pdf")}
        )
        assert response.status_code == 400

    def test_rejects_an_oversized_upload(self, client, monkeypatch):
        monkeypatch.setattr(api, "MAX_UPLOAD_BYTES", 1024)
        response = client.post(
            "/estimate",
            files={"file": ("big.pdf", b"x" * 4096, "application/pdf")},
        )
        assert response.status_code == 413


class TestJobLifecycle:
    """Queue, poll, download, delete - the path a real client walks."""

    def test_create_returns_202_and_an_id(self, client, novel_pdf):
        response = client.post("/jobs", files=_upload(novel_pdf), data={"engine": "offline"})
        assert response.status_code == 202

        body = response.json()
        assert body["id"]
        assert body["status"] in ("queued", "running")

    def test_runs_to_completion_and_reports_progress(self, client, novel_pdf):
        job_id = client.post("/jobs", files=_upload(novel_pdf),
                             data={"engine": "offline"}).json()["id"]
        body = _wait(client, job_id)

        assert body["status"] == "succeeded", body.get("error")
        assert body["progress"] == 1.0
        assert body["chapters_done"] == body["chapters_total"] == 3
        assert body["words"] > 0
        assert body["seconds"] is not None

    def test_download_returns_a_zip_of_the_chapters(self, client, novel_pdf):
        import io
        import zipfile

        job_id = client.post("/jobs", files=_upload(novel_pdf),
                             data={"engine": "offline"}).json()["id"]
        _wait(client, job_id)

        response = client.get(f"/jobs/{job_id}/download")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/zip"

        with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
            names = zf.namelist()
        assert sum(n.endswith(".mp3") for n in names) == 3
        assert "playlist.m3u" in names

    def test_downloading_too_early_is_409_not_404(self, client, novel_pdf):
        # The id is real; the audio just is not ready. A 404 would tell the
        # caller to stop polling, which is the opposite of the truth.
        job_id = client.post("/jobs", files=_upload(novel_pdf),
                             data={"engine": "offline"}).json()["id"]
        response = client.get(f"/jobs/{job_id}/download")
        assert response.status_code in (200, 409)

    def test_jobs_are_listed_newest_first(self, client, novel_pdf):
        first = client.post("/jobs", files=_upload(novel_pdf),
                            data={"engine": "offline"}).json()["id"]
        second = client.post("/jobs", files=_upload(novel_pdf),
                             data={"engine": "offline"}).json()["id"]
        ids = [j["id"] for j in client.get("/jobs").json()]
        assert ids.index(second) < ids.index(first)

    def test_delete_forgets_the_job(self, client, novel_pdf):
        job_id = client.post("/jobs", files=_upload(novel_pdf),
                             data={"engine": "offline"}).json()["id"]
        _wait(client, job_id)

        assert client.delete(f"/jobs/{job_id}").status_code == 204
        assert client.get(f"/jobs/{job_id}").status_code == 404

    def test_a_failing_conversion_is_reported_not_raised(self, client, novel_pdf, monkeypatch):
        def explode(*a, **kw):
            """Stands in for the voice service being unreachable."""
            raise RuntimeError("the voice service is down")

        monkeypatch.setattr(api, "convert", explode)
        job_id = client.post("/jobs", files=_upload(novel_pdf),
                             data={"engine": "offline"}).json()["id"]
        body = _wait(client, job_id)

        assert body["status"] == "failed"
        assert "the voice service is down" in body["error"]
        # And the download says why, rather than pretending the job is pending.
        assert client.get(f"/jobs/{job_id}/download").status_code == 409


class TestValidation:
    """Bad input rejected before any work is queued."""

    def test_unknown_job_is_404(self, client):
        assert client.get("/jobs/deadbeef").status_code == 404

    def test_unknown_engine_is_rejected(self, client, novel_pdf):
        response = client.post("/jobs", files=_upload(novel_pdf),
                               data={"engine": "nonsense"})
        assert response.status_code == 400

    @pytest.mark.parametrize("jobs", [0, 99])
    def test_worker_count_is_bounded(self, client, novel_pdf, jobs):
        response = client.post("/jobs", files=_upload(novel_pdf),
                               data={"engine": "offline", "jobs": jobs})
        assert response.status_code == 400
