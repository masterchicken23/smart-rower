#!/bin/sh
# Build and run the sender in Docker. Same shape as april-detect/run.sh.
#
#   ./run.sh              foreground, logs to the terminal
#   ./run.sh -d           detached
#
# Needs the base image from docker/build-base.sh (once).
BASE=${CAM_BASE_IMAGE:-smart-rower/raspbian-bookworm-armv6:base}
if ! docker image inspect "$BASE" >/dev/null 2>&1; then
    echo "base image $BASE not found -- run ./docker/build-base.sh first" >&2
    exit 1
fi
exec docker compose up --build "$@"
