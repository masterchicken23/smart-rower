# syntax=docker/dockerfile:1
#
# cam-sender for the Raspberry Pi Zero / Zero W (ARMv6). Optional -- the
# native install (README) is the same three apt packages without Docker.
#
# Base: Raspbian, built once on a Pi by docker/build-base.sh. The official
# debian/python armhf images are ARMv7 and will not run on a Zero.
#
# Everything comes from apt, as on the host: picamera2 and libcamera exist
# only in the Raspberry Pi archive, and pip would have to compile on a
# single-core ARMv6.

ARG BASE=smart-rower/raspbian-bookworm-armv6:base
FROM ${BASE}

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3-picamera2 libcamera-ipa python3-zmq python3-simplejpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY cam_sender ./cam_sender

# Non-root, in the image's `video` group (gid 44, as on Raspberry Pi OS), which
# owns /dev/video*, /dev/media*, /dev/vchiq and /dev/dma_heap on the host.
RUN useradd --uid 10001 --create-home --groups video cam
USER cam

# The sender touches the heartbeat while frames flow, so a camera that stops
# delivering makes the container unhealthy. Long start period: importing
# picamera2 and starting the camera takes tens of seconds on a Zero.
HEALTHCHECK --interval=15s --timeout=5s --start-period=60s --retries=3 \
    CMD python3 -c "import os,sys,time; sys.exit(time.time()-os.path.getmtime('/tmp/cam-sender.alive')>10)"

CMD ["python3", "-m", "cam_sender"]
