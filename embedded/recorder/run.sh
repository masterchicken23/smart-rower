#!/bin/sh
# Build and run the recorder. Same shape as embedded/funnel/run.sh.
#
#   ./run.sh                      foreground, logs to the terminal
#   ./run.sh -d                   detached
#   REC_DATA=/mnt/nvme/rec ./run.sh -d
#
# Then: curl -X POST localhost:8010/recorder/start   (and /stop)

# Create the data directory ourselves: if Docker creates the bind-mount source
# it is owned by root and the (non-root) recorder cannot write into it.
cd "$(dirname "$0")"
mkdir -p "${REC_DATA:-./data}"
export REC_UID="${REC_UID:-$(id -u)}" REC_GID="${REC_GID:-$(id -g)}"
exec docker compose up --build "$@"
