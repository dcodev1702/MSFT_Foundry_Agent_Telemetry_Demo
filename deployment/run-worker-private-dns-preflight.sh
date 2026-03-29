#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

SOURCE_FILE="deployment/modules/worker-resources.bicep"

if ! az account show >/dev/null 2>&1; then
  echo "ERROR: Not logged in. Run 'az login' first." >&2
  exit 1
fi

storage_endpoint_suffix="$(az cloud show --query 'suffixes.storageEndpoint' -o tsv 2>/dev/null || true)"
if [[ -z "${storage_endpoint_suffix}" ]]; then
  echo "ERROR: Unable to resolve the current Azure cloud storage endpoint suffix." >&2
  exit 1
fi

expected_suffix_line="var storageEndpointSuffix = environment().suffixes.storage"
expected_blob_line="var blobPrivateDnsZoneName = 'privatelink.blob.\${storageEndpointSuffix}'"
expected_queue_line="var queuePrivateDnsZoneName = 'privatelink.queue.\${storageEndpointSuffix}'"

contains_exact_line() {
  local source_file="$1"
  local expected_line="$2"

  python3 - "$source_file" "$expected_line" <<'PY'
from pathlib import Path
import sys

source_path = Path(sys.argv[1])
expected_line = sys.argv[2]

for raw_line in source_path.read_text(encoding='utf-8').splitlines():
    if raw_line == expected_line:
        raise SystemExit(0)

raise SystemExit(1)
PY
}

if ! contains_exact_line "${SOURCE_FILE}" "${expected_suffix_line}"; then
  echo "ERROR: Storage endpoint suffix is not derived from environment().suffixes.storage in ${SOURCE_FILE}." >&2
  exit 1
fi

if ! contains_exact_line "${SOURCE_FILE}" "${expected_blob_line}"; then
  echo "ERROR: Blob private DNS zone is not derived from environment().suffixes.storage in ${SOURCE_FILE}." >&2
  exit 1
fi

if ! contains_exact_line "${SOURCE_FILE}" "${expected_queue_line}"; then
  echo "ERROR: Queue private DNS zone is not derived from environment().suffixes.storage in ${SOURCE_FILE}." >&2
  exit 1
fi

echo "  ✓ Blob private DNS zone resolves via environment().suffixes.storage to privatelink.blob.${storage_endpoint_suffix}"
echo "  ✓ Queue private DNS zone resolves via environment().suffixes.storage to privatelink.queue.${storage_endpoint_suffix}"