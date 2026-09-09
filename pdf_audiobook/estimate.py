"""How long will this take?

Two different numbers people confuse:

* **audio length**  - how long the finished recording plays for.
* **build time**    - how long you wait while it is generated.

The neural engine produces roughly twelve seconds of audio per second of
waiting, so a 20-minute chapter costs about 100 seconds. That ratio is a
starting point only: it depends on your connection, so every conversion
records what it actually achieved and later estimates use your own measured
figure instead.
"""

from __future__ import annotations

from .extract import WORDS_PER_MINUTE

# Seconds of finished audio produced per second of waiting, for ONE worker.
# Measured here: a 24.2-minute chapter took 125s with --jobs 1 (11.6x).
DEFAULT_SPEEDUP = {"edge": 11.6, "offline": 50.0}

# Extra workers help, but not linearly - the same chapter took 48s with
# --jobs 4, which is 2.6x rather than 4x. 4 ** 0.7 = 2.64, which fits the two
# measurements taken. Rough by construction; real timings replace it as soon
# as one chapter has been converted.
CONCURRENCY_EXPONENT = 0.7

# Ignore calibration until there is enough evidence to beat the default.
MIN_CALIBRATION_WORDS = 1500


def audio_seconds(words: int, rate_pct: float = 0.0) -> float:
    """How long the finished recording will play for."""
    base = words / WORDS_PER_MINUTE * 60.0
    return base / (1.0 + rate_pct / 100.0)


def build_seconds(words: int, rate_pct: float = 0.0, engine: str = "edge",
                  speedup: float | None = None, jobs: int = 1) -> float:
    """How long you will wait for that recording to be generated."""
    if speedup is None or speedup <= 0:
        speedup = DEFAULT_SPEEDUP.get(engine, DEFAULT_SPEEDUP["edge"])
        if engine == "edge":
            speedup *= max(1, jobs) ** CONCURRENCY_EXPONENT
    return audio_seconds(words, rate_pct) / speedup


def observed_speedup(manifest: dict, engine: str | None = None,
                     jobs: int | None = None) -> float | None:
    """Your own measured throughput, from chapters already converted.

    Returns None until enough has been converted to be worth trusting.
    """
    words = 0
    audio = 0.0
    build = 0.0
    for record in manifest.get("chapters", {}).values():
        if engine and record.get("engine") not in (None, engine):
            continue
        # A run with a different worker count is not evidence about this one.
        if jobs is not None and record.get("jobs") not in (None, jobs):
            continue
        took = record.get("seconds")
        count = record.get("words")
        if not took or not count or took <= 0:
            continue
        words += count
        audio += audio_seconds(count, record.get("rate_pct", 0.0))
        build += took

    if words < MIN_CALIBRATION_WORDS or build <= 0:
        return None
    return audio / build


def humanise(seconds: float) -> str:
    """A short phrase for a duration. No hedging words - callers add those."""
    if seconds < 45:
        return "under a minute"
    minutes = seconds / 60.0
    if minutes < 90:
        count = round(minutes)
        return f"{count} minute{'s' if count != 1 else ''}"
    hours, rest = divmod(round(minutes), 60)
    if rest == 0:
        return f"{hours} hour{'s' if hours != 1 else ''}"
    return f"{hours}h {rest}m"


def describe(words: int, rate_pct: float = 0.0, engine: str = "edge",
             speedup: float | None = None, jobs: int = 1) -> str:
    """The line shown under a Convert button: wait time first, then payoff."""
    if words <= 0:
        return "Nothing to convert - everything selected is already done."
    build = humanise(build_seconds(words, rate_pct, engine, speedup, jobs))
    listen = humanise(audio_seconds(words, rate_pct))
    return (f"Approximately {build} to make the audio file "
            f"- {listen} of listening, {words:,} words")
