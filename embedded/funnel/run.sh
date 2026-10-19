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
# The rower network, shared by this project and vision/april-detect. Created
# here rather than by compose so that neither project's `down` removes it while
# the other is attached. The subnet is pinned because Docker's default pool
# (172.17-31.x) overlaps the boat AP's 172.18.248.x: containers then route
# replies to sensors onto the bridge, and SYNs arrive but SYN-ACKs never leave.
# Keep in step with vision/april-detect/run.sh.
docker network inspect rower >/dev/null 2>&1 \
    || docker network create --subnet 10.203.0.0/24 rower >/dev/null \
    || exit 1

export FUNNEL_UID="${FUNNEL_UID:-$(id -u)}" FUNNEL_GID="${FUNNEL_GID:-$(id -g)}"
exec docker compose up --build "$@"
