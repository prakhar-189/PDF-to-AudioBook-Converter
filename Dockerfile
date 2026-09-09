# Two stages, so the compiler toolchain and the pip cache never reach the
# runtime image. The venv is copied across whole - it is the only build output
# that matters.
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Dependency metadata first: this layer is cached until pyproject.toml changes,
# so ordinary source edits do not reinstall PyMuPDF every build.
COPY pyproject.toml README.md ./
COPY pdf_audiobook/__init__.py pdf_audiobook/__init__.py
RUN pip install --upgrade pip setuptools wheel && pip install ".[api,ui]"

COPY . .
RUN pip install --no-deps .


FROM python:3.12-slim AS runtime

# espeak-ng backs the `offline` engine. Without it the container can only use
# the neural voices, which need an internet connection - so this is what makes
# a fully air-gapped conversion possible. It costs a few MB.
RUN apt-get update \
    && apt-get install -y --no-install-recommends espeak-ng libespeak-ng1 \
    && rm -rf /var/lib/apt/lists/*

# Not root. The service writes only to its own temp directory, and nothing it
# does needs privileges.
RUN useradd --create-home --uid 10001 audiobook
USER audiobook

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
COPY --from=builder /opt/venv /opt/venv

WORKDIR /home/audiobook
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/healthz').read()"

# One worker on purpose. Jobs live in this process's memory and its temp
# directory, so a second worker would answer GET /jobs/{id} for jobs it has
# never heard of. Scaling out means moving the registry to Redis first - see
# the README.
CMD ["uvicorn", "pdf_audiobook.api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
