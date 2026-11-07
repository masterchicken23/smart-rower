# syntax=docker/dockerfile:1
#
# The recorder. Same shape as embedded/funnel's image: python:3.11-slim,
# dependencies before source, non-root, pure-Python wheels only (so it builds
# on the Jetson's aarch64 with no toolchain), no CUDA. It never decodes a
# frame -- JPEGs are stored as the camera sent them -- so no numpy or OpenCV.

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY recorder ./recorder

# Non-root. /data is where sessions go; compose bind-mounts the host's
# directory over it and runs as that directory's owner (see run.sh).
RUN useradd --uid 10001 --create-home recorder \
    && mkdir -p /data \
    && chown -R recorder:recorder /data /app
USER recorder

EXPOSE 8010

# Liveness of the process, not of the sources: a boat with its sensors off
# must not get a healthy recorder restarted (the funnel's /livez rule).
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request as u; u.urlopen('http://127.0.0.1:%s/recorder/livez' % os.environ.get('REC_HTTP_PORT','8010'), timeout=2)"

CMD ["python", "-m", "recorder"]
