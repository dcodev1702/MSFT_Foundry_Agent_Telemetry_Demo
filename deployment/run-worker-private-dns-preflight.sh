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

if ! rg -Fqx "${expected_suffix_line}" "${SOURCE_FILE}"; then
  echo "ERROR: Storage endpoint suffix is not derived from environment().suffixes.storage in ${SOURCE_FILE}." >&2
  exit 1
fi

if ! rg -Fqx "${expected_blob_line}" "${SOURCE_FILE}"; then
  echo "ERROR: Blob private DNS zone is not derived from environment().suffixes.storage in ${SOURCE_FILE}." >&2
  exit 1
fi

if ! rg -Fqx "${expected_queue_line}" "${SOURCE_FILE}"; then
  echo "ERROR: Queue private DNS zone is not derived from environment().suffixes.storage in ${SOURCE_FILE}." >&2
  exit 1
fi

echo "  ✓ Blob private DNS zone resolves via environment().suffixes.storage to privatelink.blob.${storage_endpoint_suffix}"
echo "  ✓ Queue private DNS zone resolves via environment().suffixes.storage to privatelink.queue.${storage_endpoint_suffix}"