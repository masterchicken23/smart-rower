#!/bin/sh
# Build and run the four detectors. Same shape as embedded/funnel/run.sh.
#
#   ./run.sh                      foreground, logs to the terminal
#   ./run.sh -d                   detached
#   ./run.sh april-cam1           one camera only
exec docker compose up --build "$@"
