# syntax=docker/dockerfile:1
#
# april-detect, CPU detector. One container per camera.
#
# Same shape as embedded/funnel's image -- python:3.11-slim, deps before
# source, non-root, no CUDA base -- with one addition: a builder stage,
# because pupil-apriltags publishes no aarch64 wheel and has to be compiled on
# the Jetson. The toolchain stays in the builder; the runtime stage installs
# only the finished wheel.
#
# For the cuAprilTags (GPU) detector use Dockerfile.cuda instead.

FROM python:3.11-slim AS builder
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential cmake git \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/
# Wheels for everything; on x86 this just downloads, on aarch64 it compiles
# pupil-apriltags once.
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r /tmp/requirements.txt

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY --from=builder /wheels /wheels
COPY requirements.txt ./
RUN pip install --no-cache-dir --no-index --find-links /wheels -r requirements.txt \
    && rm -rf /wheels

COPY april_detect ./april_detect
COPY tools ./tools

# Non-root. /config is where calibration files are mounted read-only.
RUN useradd --uid 10001 --create-home april \
    && mkdir -p /config \
    && chown -R april:april /app
USER april

# Liveness, not data: the main loop touches the heartbeat every second whether
# or not frames are arriving, so a camera that is switched off does not get a
# healthy detector restarted -- the same distinction the funnel draws between
# /livez and /health.
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import os,sys,time; sys.exit(time.time()-os.path.getmtime('/tmp/april-detect.alive')>10)"

CMD ["python", "-m", "april_detect"]
