#!/usr/bin/env bash
set -euo pipefail

gateway_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$gateway_dir/.." && pwd)"
python_bin="${PYTHON_BIN:-$repo_root/.venv-linux/bin/python}"

if [[ ! -x "$python_bin" ]]; then
    printf 'Python environment not found: %s\n' "$python_bin" >&2
    exit 1
fi

"$python_bin" "$gateway_dir/refresh_runtime_env.py" "$@"
docker compose --project-directory "$gateway_dir" --env-file "$gateway_dir/.env" config --quiet
docker compose --project-directory "$gateway_dir" --env-file "$gateway_dir/.env" up -d

container_id="$(
    docker compose \
        --project-directory "$gateway_dir" \
        --env-file "$gateway_dir/.env" \
        ps --quiet litellm
)"
if [[ -z "$container_id" ]]; then
    printf 'LiteLLM container was not created.\n' >&2
    exit 1
fi

health_status="starting"
for _ in {1..36}; do
    health_status="$(docker inspect --format '{{.State.Health.Status}}' "$container_id")"
    case "$health_status" in
        healthy)
            break
            ;;
        unhealthy)
            printf 'LiteLLM became unhealthy during startup.\n' >&2
            docker compose \
                --project-directory "$gateway_dir" \
                --env-file "$gateway_dir/.env" \
                ps
            exit 1
            ;;
    esac
    sleep 5
done

if [[ "$health_status" != "healthy" ]]; then
    printf 'LiteLLM did not become healthy within 180 seconds.\n' >&2
    docker compose \
        --project-directory "$gateway_dir" \
        --env-file "$gateway_dir/.env" \
        ps
    exit 1
fi

database_status="unknown"
for _ in {1..12}; do
    if database_status="$(
        docker exec "$container_id" python -c '
import json
import urllib.request

with urllib.request.urlopen("http://127.0.0.1:4000/health/readiness", timeout=5) as response:
    print(json.load(response).get("db", "unknown"))
' 2>/dev/null
    )" && [[ "$database_status" == "connected" ]]; then
        break
    fi
    database_status="disconnected"
    sleep 5
done

if [[ "$database_status" != "connected" ]]; then
    printf 'LiteLLM is running, but it cannot connect to Neon. Update DATABASE_URL with the current direct connection string.\n' >&2
    exit 1
fi
printf 'Neon database: connected\n'

docker compose --project-directory "$gateway_dir" --env-file "$gateway_dir/.env" ps
