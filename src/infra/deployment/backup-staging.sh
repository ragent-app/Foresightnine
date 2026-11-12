#!/usr/bin/env bash
set -euo pipefail
umask 077

# Create a repository-external backup of the local/staging Compose services.
# The dumps contain private data and are NOT redacted. The script never prints
# database credentials and has no restore mode.

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd -- "$script_dir/../../.." && pwd -P)
compose_file="$repo_root/src/infra/docker/compose.yaml"

if [[ $# != 1 ]]; then
  echo "usage: $0 /absolute/path/outside/repository" >&2
  exit 2
fi

output_dir=$1
if [[ "$output_dir" != /* ]]; then
  echo "backup output must be an absolute path outside the repository" >&2
  exit 2
fi
output_dir=$(realpath -m -- "$output_dir")
if [[ "$output_dir" == "$repo_root" || "$output_dir" == "$repo_root"/* ]]; then
  echo "backup output must be an absolute path outside the repository" >&2
  exit 2
fi

mkdir -p -- "$output_dir"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)-$$"
postgres_dump="$output_dir/postgres-$timestamp.dump"
redis_dump="$output_dir/redis-$timestamp.rdb"
checksums="$output_dir/sha256-$timestamp.txt"
redis_tmp="/tmp/foresightnine-redis-$timestamp.rdb"
cleanup() {
  local status=$?
  docker compose -f "$compose_file" exec -T redis rm -f -- "$redis_tmp" >/dev/null 2>&1 || true
  if (( status != 0 )); then
    rm -f -- "$postgres_dump" "$redis_dump" "$checksums"
  fi
}
trap cleanup EXIT

docker compose -f "$compose_file" exec -T postgres sh -c \
  'pg_dump --format=custom --no-owner --no-acl -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
  > "$postgres_dump"

# redis-cli --rdb writes the snapshot inside the container; docker compose cp
# moves only that binary backup to the owner-selected external directory.
docker compose -f "$compose_file" exec -T redis redis-cli --rdb "$redis_tmp" >/dev/null
docker compose -f "$compose_file" cp "redis:$redis_tmp" "$redis_dump"

sha256sum "$postgres_dump" "$redis_dump" > "$checksums"
chmod 600 "$postgres_dump" "$redis_dump" "$checksums"
printf 'backup_created postgres=%s redis=%s checksums=%s\n' \
  "$postgres_dump" "$redis_dump" "$checksums"
