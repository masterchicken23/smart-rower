#!/bin/sh
# Build and run the four detectors. Same shape as embedded/funnel/run.sh.
#
#   ./run.sh                      foreground, logs to the terminal
#   ./run.sh -d                   detached
#   ./run.sh april-cam1           one camera only
cd "$(dirname "$0")"

# The rower network, shared by this project and embedded/funnel. Created
# here rather than by compose so that neither project's `down` removes it while
# the other is attached. The subnet is pinned because Docker's default pool
# (172.17-31.x) overlaps the boat AP's 172.18.248.x: containers then route
# replies to sensors onto the bridge, and SYNs arrive but SYN-ACKs never leave.
# Keep in step with embedded/funnel/run.sh.
docker network inspect rower >/dev/null 2>&1 \
    || docker network create --subnet 10.203.0.0/24 rower >/dev/null \
    || exit 1

exec docker compose up --build "$@"
