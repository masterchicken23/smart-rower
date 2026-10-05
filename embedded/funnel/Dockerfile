# syntax=docker/dockerfile:1
#
# The funnel. Pure-Python dependencies only, so this builds on both an x86
# laptop and the Jetson's aarch64 without a toolchain and without touching
# JetPack's system Python.
#
# No GPU access is needed -- the computer-vision work happens in a separate
# process that pipes its results in -- so this image wants no CUDA base and the
# compose service needs no `runtime: nvidia`.

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first, so editing the source does not re-resolve them.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY funnel ./funnel
COPY tools ./tools

# Non-root. /data is the recording volume; it is only written when
# FUNNEL_RECORD_ENABLED=true, but it must be ownable either way.
RUN useradd --uid 10001 --create-home funnel \
    && mkdir -p /data \
    && chown -R funnel:funnel /data /app
USER funnel

EXPOSE 8000

# /livez, not /health: /health answers 503 when no sensor data is arriving,
# which is a true statement about the data and a false one about the process.
# Probing it would restart a healthy funnel every time the sensors were off.
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/livez', timeout=2)"

CMD ["python", "-m", "funnel"]
