#!/usr/bin/env bash
set -euo pipefail

gateway_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$gateway_dir/.." && pwd)"
python_bin="${PYTHON_BIN:-$repo_root/.venv-linux/bin/python}"
env_file="$gateway_dir/.env"

if [[ ! -f "$env_file" ]]; then
    printf 'Missing %s; run gateway/start.sh first.\n' "$env_file" >&2
    exit 1
fi

read_env() {
    "$python_bin" - "$env_file" "$1" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
key = sys.argv[2]
for raw_line in path.read_text(encoding="utf-8").splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    candidate, value = line.split("=", 1)
    if candidate.strip() != key:
        continue
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    print(value)
    raise SystemExit(0)
raise SystemExit(f"Missing {key} in {path}")
PY
}

master_key="$(read_env LITELLM_MASTER_KEY)"
port="$(read_env LITELLM_PORT)"
response_file="$(mktemp)"
trap 'rm -f -- "$response_file"' EXIT

# Usage: post ROUTE BODY EXPECTATION, where EXPECTATION is "response" or "conversation".
post() {
    local status_code
    status_code="$(
        curl --silent --show-error \
            --output "$response_file" \
            --write-out '%{http_code}' \
            "http://127.0.0.1:${port}$1" \
            --header "Authorization: Bearer $master_key" \
            --header 'Content-Type: application/json' \
            --data "$2"
    )"
    "$python_bin" - "$response_file" "$status_code" "$1" "$3" <<'PY'
import json
import sys
from pathlib import Path

path, status_code, route, expectation = Path(sys.argv[1]), int(sys.argv[2]), sys.argv[3], sys.argv[4]
payload = json.loads(path.read_text(encoding="utf-8"))
if status_code != 200:
    error = payload.get("error", {})
    raise SystemExit(f"{route} returned HTTP {status_code}: {error.get('message', payload)}")
if expectation == "response" and (payload.get("status") != "completed" or not payload.get("output")):
    raise SystemExit(f"{route} did not complete with output")
if expectation == "conversation" and (payload.get("object") != "conversation" or not payload.get("id")):
    raise SystemExit(f"{route} did not create a conversation")
print(f"PASS {route}")
PY
}

post /foundry-agent/main/responses '{"input":"Reply with the single word READY."}' response
post /foundry-agent/sentinel/conversations '{}' conversation
printf 'LiteLLM -> Foundry agent smoke test passed.\n'
