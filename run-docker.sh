#!/usr/bin/env sh
# Run wp-static-export inside a throwaway Python container -- for hosts
# whose system Python is older than 3.10 (CentOS/RHEL 7, old Debian, ...).
# All arguments are passed straight to wp-static-export.py:
#
#   ./run-docker.sh https://www.example.at -o ./export --clean
#   ./run-docker.sh http://10.0.0.5:8080 --host example.at -o ./export
#
# --network host so internal origin IPs stay reachable; the container runs
# with YOUR uid, so the export files in ./ belong to you, not root.
set -eu
cd "$(dirname "$0")"

command -v docker >/dev/null 2>&1 || {
  echo "error: docker not found -- install it first:" \
       "https://docs.docker.com/get-docker/" >&2
  exit 1
}

exec docker run --rm -i \
  --network host \
  -u "$(id -u):$(id -g)" -e HOME=/tmp \
  -v "$(pwd):/work" -w /work \
  --entrypoint sh python:3.12-slim -c \
  'pip install -q --user --no-warn-script-location -r requirements.txt \
     && exec python wp-static-export.py "$@"' sh "$@"
