#!/usr/bin/env bash
# Build, smoke-test, then publish one native Linux architecture to Docker Hub.
set -euo pipefail
repo="${1:-chirophy/fxwatch}"
version="${2:-1.3.0}"
if [[ ! "$repo" =~ ^[a-z0-9][a-z0-9_-]*/[a-z0-9][a-z0-9_.-]*$ ]] || \
   [[ ! "$version" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]*$ ]]; then
  echo 'Invalid Docker Hub repository or tag.' >&2
  exit 1
fi
source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
image="$repo:$version"
docker info >/dev/null
docker build -t "$image" "$source_dir"

# Anonymous scratch volume; never mount the production fxwatch-data here.
test_container=''
cleanup() {
  if [[ -n "$test_container" ]]; then
    docker rm -f -v "$test_container" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT
test_container="$(docker run -d --stop-timeout 90 --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m,mode=1777 \
  --cap-drop ALL --security-opt no-new-privileges:true \
  --mount type=volume,destination=/data "$image")"
ready=false
for ((attempt=0; attempt<60; attempt++)); do
  state="$(docker inspect --format '{{.State.Status}}' "$test_container")"
  if [[ "$state" != running ]]; then
    break
  fi
  if docker exec "$test_container" python -c \
    'import json,urllib.request; d=json.load(urllib.request.urlopen("http://127.0.0.1:8099/api/summary",timeout=3)); assert {c["code"] for c in d["currencies"]}=={"USD","EUR","HKD","JPY"}' >/dev/null 2>&1; then
    ready=true
    break
  fi
  sleep 1
done
if [[ "$ready" != true ]]; then
  docker logs --tail 80 "$test_container" >&2
  echo 'Smoke test failed; image was not pushed.' >&2
  exit 1
fi
# Recheck after a short interval so an immediate child failure is not missed.
sleep 2
[[ "$(docker inspect --format '{{.State.Status}}' "$test_container")" == running ]]
docker stop --time 90 "$test_container" >/dev/null
[[ "$(docker inspect --format '{{.State.ExitCode}}' "$test_container")" == 0 ]]
cleanup
test_container=''
docker push "$image"
echo "Published $image for the build machine architecture."
