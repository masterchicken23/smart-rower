#!/bin/sh
# Build and run the funnel plus its broker. Same shape as the run.sh scripts
# beside the other experiments in this repo, but the service itself is
# containerised, so this is the whole deployment story.
#
#   ./run.sh                      foreground, logs to the terminal
#   ./run.sh -d                   detached
#   FUNNEL_RECORD_ENABLED=true ./run.sh    debug run, recording to ./data

# Create ./data ourselves: if Docker creates the bind-mount source it is owned
# by root and the (non-root) funnel cannot record into it.
cd "$(dirname "$0")"
mkdir -p data
export FUNNEL_UID="${FUNNEL_UID:-$(id -u)}" FUNNEL_GID="${FUNNEL_GID:-$(id -g)}"
exec docker compose up --build "$@"
