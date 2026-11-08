#!/bin/sh
# Build and run the sender in Docker. Same shape as april-detect/run.sh.
#
#   ./run.sh              foreground, logs to the terminal
#   ./run.sh -d           detached
#
# Needs the base image from docker/build-base.sh (once).
cd "$(dirname "$0")"
BASE=${CAM_BASE_IMAGE:-smart-rower/raspbian-bookworm-armv6:base}
if ! docker image inspect "$BASE" >/dev/null 2>&1; then
    echo "base image $BASE not found -- run ./docker/build-base.sh first" >&2
    exit 1
fi
# The local recording's directory (CAM_REC_DATA, mounted at /rec). Created
# here: if Docker creates a bind-mount source it is owned by root, and the
# container's user could not write into it.
REC_DATA=${CAM_REC_DATA:-./rec}
mkdir -p "$REC_DATA"
if [ "$(stat -c %u "$REC_DATA" 2>/dev/null)" != 10001 ]; then
    echo "note: with CAM_RECORD=true the container (uid 10001) must own $REC_DATA:" >&2
    echo "      sudo chown 10001:10001 $REC_DATA" >&2
fi
exec docker compose up --build "$@"
