#!/usr/bin/env bash
# Check the six publish secrets BEFORE tagging a release - nothing is published or pushed.
#
#   1. Helin Platform API : M2M token for HELIN_INSTANCE (HELIN_CLIENT_ID / _SECRET)
#   2. Module             : read module_uuid from module.yaml back from the API
#   3. Registry           : docker login to REGISTRY_URL (REGISTRY_USER / _PASSWORD), then logout
#   4. Publish payload    : the JSON publish-version.sh would send, built locally (not sent)
#
# Uses the same variable names as the GitHub Action secrets; put them in .env and run
# `make preflight` (or export them and run this script). Secrets are never printed.
set -uo pipefail
cd "$(dirname "$0")/.."

ok()   { echo "  OK    $*"; }
fail() { echo "  FAIL  $*"; failed=1; }
failed=0

for var in HELIN_INSTANCE HELIN_CLIENT_ID HELIN_CLIENT_SECRET REGISTRY_URL REGISTRY_USER REGISTRY_PASSWORD; do
  [ -n "${!var:-}" ] || fail "$var is not set"
done
[ "$failed" = 0 ] || exit 1

echo "1. Helin Platform API ($HELIN_INSTANCE)"
if token=$(scripts/get-token.sh 2>/tmp/preflight-token.err) && [ -n "$token" ] && [ "$token" != "null" ]; then
  ok "M2M token issued (${#token} chars)"
else
  fail "no token: $(tail -1 /tmp/preflight-token.err)"
fi

echo "2. Module in module.yaml"
uuid=$(grep '^module_uuid:' module.yaml | cut -d ':' -f 2- | tr -d ' ')
if [ -z "$uuid" ]; then
  fail "no module_uuid in module.yaml - run make register first"
elif [ -n "${token:-}" ]; then
  for path in "modules/$uuid" "modules/$uuid/versions"; do
    code=$(curl -s -o /tmp/preflight-module.json -w '%{http_code}' \
      -H "authorization: Bearer $token" "$HELIN_INSTANCE/api/v1/$path")
    if [ "$code" = 200 ]; then
      ok "GET /api/v1/$path -> 200 $(jq -c '{name, meta_name, id} // (if type=="array" then {versions: length} else . end)' /tmp/preflight-module.json 2>/dev/null | head -c 160)"
    else
      echo "  INFO  GET /api/v1/$path -> HTTP $code (endpoint may not exist; publish uses POST .../versions)"
    fi
  done
fi

echo "3. Container registry ($REGISTRY_URL)"
if printf '%s' "$REGISTRY_PASSWORD" | docker login "$REGISTRY_URL" --username "$REGISTRY_USER" --password-stdin >/tmp/preflight-docker.log 2>&1; then
  ok "docker login succeeded"
  docker logout "$REGISTRY_URL" >/dev/null 2>&1 && ok "logged out again (no credentials left on this machine)"
else
  fail "docker login refused: $(grep -iE 'error|denied|unauthorized' /tmp/preflight-docker.log | tail -1)"
fi

echo "4. Publish payload (built locally, NOT sent)"
meta_name=$(grep '^meta_name:' module.yaml | cut -d ':' -f 2- | tr -d ' ')
if jq --arg version "0.0.0-preflight" --arg image_name "$REGISTRY_URL/$meta_name" --arg image_tag "0.0.0-preflight" \
     '. + {version: $version, image_name: $image_name, image_tag: $image_tag}' module_metadata.json >/tmp/preflight-payload.json; then
  ok "image $REGISTRY_URL/$meta_name, Cmd: $(jq -c '.container_template.Cmd' /tmp/preflight-payload.json)"
  ok "binds: $(jq -c '.container_template.HostConfig.Binds' /tmp/preflight-payload.json)"
else
  fail "module_metadata.json is not valid JSON"
fi

rm -f /tmp/preflight-token.err /tmp/preflight-module.json /tmp/preflight-docker.log
echo
if [ "$failed" = 0 ]; then
  echo "PREFLIGHT PASSED - the same values as GitHub secrets will let publish.yml run."
else
  echo "PREFLIGHT FAILED - fix the values above before setting the GitHub secrets / tagging."
  exit 1
fi
