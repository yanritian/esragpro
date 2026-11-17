#!/usr/bin/env bash
set -euo pipefail

command_name=""
project_name="${COMPOSE_PROJECT_NAME:-}"
compose_file="${COMPOSE_FILE:-docker-compose.yml}"
host_source="${MODEL_SOURCE:-}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        acquire|import|verify)
            [[ -z "$command_name" ]] || { printf 'command specified more than once\n' >&2; exit 2; }
            command_name="$1"
            shift
            ;;
        --project-name) project_name="$2"; shift 2 ;;
        --compose-file) compose_file="$2"; shift 2 ;;
        --source) host_source="$2"; shift 2 ;;
        --secure) compose_file="docker-compose.secure.yml"; shift ;;
        *) printf 'unknown option: %s\n' "$1" >&2; exit 2 ;;
    esac
done
[[ -n "$command_name" ]] || command_name="verify"
case "$command_name" in
    acquire|import|verify) ;;
    *) printf 'command must be acquire, import, or verify\n' >&2; exit 2 ;;
esac
if [[ "$command_name" == "import" && -z "$host_source" ]]; then
    printf '%s\n' '--source is required for import' >&2
    exit 2
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
compose_path="$repo_root/$compose_file"
[[ -f "$compose_path" ]] || { printf 'Compose file not found: %s\n' "$compose_path" >&2; exit 2; }

compose_args=()
[[ -n "$project_name" ]] && compose_args+=(-p "$project_name")
compose_args+=(-f "$compose_path" run --rm --no-deps)
if [[ -n "$host_source" ]]; then
    [[ -e "$host_source" ]] || { printf 'model source not found\n' >&2; exit 2; }
    compose_args+=(-v "$host_source:/mnt/model-source:ro")
fi
compose_args+=(
    embedding-model-init
    "$command_name"
    --lock /app/model-locks/BAAI--bge-m3.json
    --model-root /models
)
[[ "$command_name" == "import" ]] && compose_args+=(--source /mnt/model-source)
cd "$repo_root"
docker compose "${compose_args[@]}"
