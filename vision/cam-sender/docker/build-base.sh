#!/bin/sh
# Build the ARMv6 base image the cam-sender Dockerfile starts FROM. One-off.
#
#   sudo apt install -y debootstrap
#   ./docker/build-base.sh                      # tag: smart-rower/raspbian-bookworm-armv6:base
#
# Why this exists: the official debian and python "armhf" images are built for
# ARMv7 and die with "Illegal instruction" on an original Pi Zero / Zero W
# (ARMv6). Raspbian is the Debian rebuild for ARMv6 hard-float that Raspberry
# Pi OS 32-bit is made from, and nobody publishes a maintained Docker image of
# it with the Raspberry Pi archive (where picamera2 and libcamera live). So
# this bootstraps a minimal one from the official mirrors and imports it.
#
# Run it on any 32-bit Raspberry Pi OS host -- the Zero itself works but takes
# a while; a Pi 3/4 running 32-bit Raspberry Pi OS is much faster and its
# output runs on the Zero unchanged. Move it with:
#   docker save smart-rower/raspbian-bookworm-armv6:base | ssh <zero> docker load
set -eu

TAG=${1:-smart-rower/raspbian-bookworm-armv6:base}
SUITE=bookworm
RASPBIAN_KEY=/usr/share/keyrings/raspbian-archive-keyring.gpg
RPI_KEY=/usr/share/keyrings/raspberrypi-archive-keyring.gpg

for k in "$RASPBIAN_KEY" "$RPI_KEY"; do
    [ -f "$k" ] || { echo "missing $k -- run this on 32-bit Raspberry Pi OS" >&2; exit 1; }
done
command -v debootstrap >/dev/null || { echo "sudo apt install -y debootstrap" >&2; exit 1; }

ROOT=$(mktemp -d)
trap 'sudo rm -rf "$ROOT"' EXIT

sudo debootstrap --arch=armhf --variant=minbase --keyring="$RASPBIAN_KEY" \
    --include=ca-certificates "$SUITE" "$ROOT" http://raspbian.raspberrypi.com/raspbian/

# The Raspberry Pi archive: picamera2, libcamera, simplejpeg, built for ARMv6.
sudo install -m 644 "$RPI_KEY" "$ROOT$RPI_KEY"
echo "deb [signed-by=$RPI_KEY] http://archive.raspberrypi.com/debian/ $SUITE main" \
    | sudo tee "$ROOT/etc/apt/sources.list.d/raspi.list" >/dev/null
sudo chroot "$ROOT" sh -c 'apt-get clean && rm -rf /var/lib/apt/lists/* /var/cache/apt/*'

sudo tar -C "$ROOT" -c . | docker import \
    --change 'ENV DEBIAN_FRONTEND=noninteractive' \
    --change 'CMD ["/bin/bash"]' \
    - "$TAG"
echo "built $TAG"
