#!/bin/sh
# Build and run the funnel plus its broker. Same shape as the run.sh scripts
# beside the other experiments in this repo, but the service itself is
# containerised, so this is the whole deployment story.
#
#   ./run.sh                      foreground, logs to the terminal
#   ./run.sh -d                   detached
#   FUNNEL_RECORD_ENABLED=true ./run.sh    debug run, recording to ./data
exec docker compose up --build "$@"
