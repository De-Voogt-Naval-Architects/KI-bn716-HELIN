#!/usr/bin/env bash
# One-time module registration: POST /modules from module.yaml, then write the
# returned module_id and module_uuid back into module.yaml (commit them).
#
# Idempotent: if module.yaml already contains a module_id, this is a no-op.
#
# Required environment variables (same as scripts/get-token.sh):
#   HELIN_INSTANCE, HELIN_CLIENT_ID, HELIN_CLIENT_SECRET
# Stop on the first problem instead of continuing with broken data:
# -e exit on any failed command, -u treat unset variables as errors,
# -o pipefail a pipeline fails if any command in it fails (not just the last).
set -euo pipefail

cd "$(dirname "$0")/.."

err() { echo "[register] ERROR: $*" >&2; }

if grep -q '^module_id:' module.yaml; then
  echo "[register] skipped - module.yaml already has a module_id"
  exit 0
fi

# Read one value from module.yaml. Values are taken literally, single line only.
field() {
  line=$(grep "^$1:" module.yaml || true)      # the whole "key: value" line ("" if missing)
  echo "$line" | cut -d ':' -f 2- | sed 's/^ *//; s/ *$//'  # value after the first ":", trimmed
}

name=$(field name)
meta_name=$(field meta_name)
author=$(field author)
description=$(field description)
logo_url=$(field logo_url)
category_id=$(field category_id)

for f in name meta_name author category_id; do
  if [ -z "${!f}" ]; then
    err "'$f' is missing in module.yaml"
    exit 1
  fi
done

token=$(scripts/get-token.sh)

payload=$(jq -n \
  --arg name "$name" \
  --arg meta_name "$meta_name" \
  --arg author "$author" \
  --arg description "$description" \
  --arg logo_url "$logo_url" \
  --arg category_id "$category_id" \
  '{name: $name, meta_name: $meta_name, author: $author, description: $description, logo_url: $logo_url, category_id: $category_id}')

response=$(curl --silent --show-error --fail-with-body \
  --header "content-type: application/json" \
  --header "authorization: Bearer $token" \
  --data "$payload" \
  "$HELIN_INSTANCE/api/v1/modules") \
  || { err "POST /modules failed: $response"; exit 1; }

module_id=$(jq -r '.id' <<<"$response")
module_uuid=$(jq -r '.uuid' <<<"$response")

# check if we received correctly both id and uuid 
if [ -z "$module_id" ] || [ "$module_id" = "null" ] || [ -z "$module_uuid" ] || [ "$module_uuid" = "null" ]; then
  err "unexpected response from POST /modules: $response"
  exit 1
fi

printf 'module_id: %s\nmodule_uuid: %s\n' "$module_id" "$module_uuid" >> module.yaml

echo "[register] Module '$name' registered (module_id: $module_id)."
echo "[register] module.yaml was updated - commit it so CI can publish versions."
