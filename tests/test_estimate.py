"""The wait-time estimate, and the self-calibration behind it."""

from __future__ import annotations

import pytest

from pdf_audiobook.estimate import (
    MIN_CALIBRATION_WORDS,
    audio_seconds,
    build_seconds,
    describe,
    humanise,
    observed_speedup,
)


class TestAudioSeconds:
    """How long the finished recording plays for."""

    def test_uses_the_measured_words_per_minute(self):
        assert audio_seconds(180) == pytest.approx(60.0)

    def test_a_faster_rate_shortens_the_recording(self):
        assert audio_seconds(180, rate_pct=100) == pytest.approx(30.0)

    def test_a_slower_rate_lengthens_it(self):
        assert audio_seconds(180, rate_pct=-50) == pytest.approx(120.0)


class TestBuildSeconds:
    """How long you wait while it is generated."""

    def test_explicit_speedup_wins(self):
        # 60s of audio at 10x real time is 6s of waiting.
        assert build_seconds(180, speedup=10.0) == pytest.approx(6.0)

    def test_more_workers_means_less_waiting(self):
        assert build_seconds(10_000, jobs=4) < build_seconds(10_000, jobs=1)

    def test_extra_workers_help_sub_linearly(self):
        # 4 workers measured 2.6x, not 4x. Promising 4x would make every
        # estimate on the Convert button optimistic.
        one = build_seconds(10_000, jobs=1)
        four = build_seconds(10_000, jobs=4)
        assert 2.0 < one / four < 3.5

    def test_offline_engine_is_much_faster_to_build(self):
        assert build_seconds(10_000, engine="offline") < build_seconds(10_000, engine="edge")

    def test_a_nonsense_speedup_falls_back_to_the_default(self):
        assert build_seconds(1000, speedup=0) == build_seconds(1000)
        assert build_seconds(1000, speedup=-5) == build_seconds(1000)


class TestObservedSpeedup:
    """Self-calibration, and the guards on what counts as evidence.

    A run at a different engine or worker count says nothing about this one,
    so folding it in would make every later estimate worse, not better.
    """

    def test_none_until_there_is_enough_evidence(self):
        manifest = {"chapters": {"1": {"words": 100, "seconds": 10.0}}}
        assert observed_speedup(manifest) is None

    def test_computes_throughput_once_calibrated(self):
        words = MIN_CALIBRATION_WORDS * 2
        # audio_seconds(words) of audio produced in 10s of waiting.
        manifest = {"chapters": {"1": {"words": words, "seconds": 10.0, "rate_pct": 0.0}}}
        assert observed_speedup(manifest) == pytest.approx(audio_seconds(words) / 10.0)

    def test_ignores_runs_from_a_different_engine(self):
        manifest = {"chapters": {
            "1": {"words": MIN_CALIBRATION_WORDS * 2, "seconds": 10.0, "engine": "offline"},
        }}
        assert observed_speedup(manifest, engine="edge") is None

    def test_ignores_runs_with_a_different_worker_count(self):
        # A 4-worker run says nothing about how fast 1 worker will be.
        manifest = {"chapters": {
            "1": {"words": MIN_CALIBRATION_WORDS * 2, "seconds": 10.0, "jobs": 4},
        }}
        assert observed_speedup(manifest, jobs=1) is None

    def test_skips_incomplete_records(self):
        manifest = {"chapters": {
            "1": {"words": MIN_CALIBRATION_WORDS * 2},          # never timed
            "2": {"seconds": 10.0},                              # no word count
            "3": {"words": 500, "seconds": 0},                   # zero duration
        }}
        assert observed_speedup(manifest) is None

    def test_empty_manifest_is_safe(self):
        assert observed_speedup({}) is None


class TestHumanise:
    """Durations phrased the way a person would say them."""

    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            (5, "under a minute"),
            (44, "under a minute"),
            (60, "1 minute"),
            (600, "10 minutes"),
            (7200, "2 hours"),
            (9000, "2h 30m"),
        ],
    )
    def test_reads_like_a_person_wrote_it(self, seconds, expected):
        assert humanise(seconds) == expected


class TestDescribe:
    """The sentence shown under every Convert button."""

    def test_leads_with_the_wait_then_the_payoff(self):
        line = describe(34_078)
        assert line.startswith("Approximately")
        assert "of listening" in line
        assert "34,078 words" in line

    def test_says_so_when_there_is_nothing_to_do(self):
        assert "already done" in describe(0)
        assert "already done" in describe(-5)
