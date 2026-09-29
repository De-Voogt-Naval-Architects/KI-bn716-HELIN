#!/usr/bin/env bash
# Publish a new module version: POST /modules/{module_uuid}/versions.
# Run by CI on every version tag; can also be run manually.
#
# Usage: scripts/publish-version.sh <version> <image_name> <image_tag>
#
# The deploy-time templates are read from module_metadata.json; the module to
# publish to comes from the module_uuid in module.yaml (written by `make register`).
#
# Required environment variables:
#   HELIN_INSTANCE, HELIN_CLIENT_ID, HELIN_CLIENT_SECRET
set -euo pipefail

cd "$(dirname "$0")/.."

err() { echo "[publish-version] ERROR: $*" >&2; }

version="${1:-}"
image_name="${2:-}"
image_tag="${3:-}"
if [ -z "$version" ] || [ -z "$image_name" ] || [ -z "$image_tag" ]; then
  err "usage: scripts/publish-version.sh <version> <image_name> <image_tag>"
  exit 1
fi

line=$(grep '^module_uuid:' module.yaml || true)
module_uuid=$(echo "$line" | cut -d ':' -f 2- | sed 's/^ *//; s/ *$//')
if [ -z "$module_uuid" ]; then
  err "no module_uuid in module.yaml - run 'make register' once and commit module.yaml"
  exit 1
fi

token=$(scripts/get-token.sh)

# The request body is module_metadata.json plus the version fields.
payload=$(jq \
  --arg version "$version" \
  --arg image_name "$image_name" \
  --arg image_tag "$image_tag" \
  '. + {version: $version, image_name: $image_name, image_tag: $image_tag}' \
  module_metadata.json)

response=$(curl --silent --show-error --fail-with-body \
  --header "content-type: application/json" \
  --header "authorization: Bearer $token" \
  --data "$payload" \
  "$HELIN_INSTANCE/api/v1/modules/$module_uuid/versions") \
  || { err "POST /modules/$module_uuid/versions failed: $response"; exit 1; }

echo "[publish-version] Version $version published (image $image_name:$image_tag)."
