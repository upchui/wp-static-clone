#!/usr/bin/env sh
# Run wp-static-export inside a throwaway Python container -- for hosts
# whose system Python is older than 3.10 (CentOS/RHEL 7, old Debian, ...).
# All arguments are passed straight to wp-static-export.py, and relative
# paths (-o ./static) resolve in YOUR current directory, not the repo:
#
#   /opt/wp-static-clone/run-docker.sh https://www.example.at -o ./export --clean
#
# --network host so internal origin IPs stay reachable; the container runs
# with YOUR uid, so the export files belong to you, not root.
set -eu

command -v docker >/dev/null 2>&1 || {
  echo "error: docker not found -- install it first:" \
       "https://docs.docker.com/get-docker/" >&2
  exit 1
}

# the repo (script + requirements) is mounted read-only under /tool; the
# caller's working directory is the container's working directory
REPO=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

exec docker run --rm -i \
  --network host \
  -u "$(id -u):$(id -g)" -e HOME=/tmp \
  -v "$REPO:/tool:ro" \
  -v "$(pwd):/work" -w /work \
  --entrypoint sh python:3.12-slim -c \
  'pip install -q --user --no-warn-script-location -r /tool/requirements.txt \
     && exec python /tool/wp-static-export.py "$@"' sh "$@"
