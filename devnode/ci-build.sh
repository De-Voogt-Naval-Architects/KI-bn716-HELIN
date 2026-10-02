#!/usr/bin/env bash
# Reproduce the CI image build (publish.yml / build.yml) in the VM: buildx with a
# docker-container builder, linux/amd64 + linux/arm64, no push. Then load the amd64
# image as datalogger-extractor:ci for running.
# The office's TLS inspection re-signs Docker Hub, so BuildKit gets the company CA.
set -euo pipefail
cd "${REPO_DIR:-$HOME/app}"
CA=/usr/local/share/ca-certificates/devnode-corp-ca.crt
cat > /tmp/buildkitd.toml <<EOF
[registry."docker.io"]
  ca = ["$CA"]
[registry."registry-1.docker.io"]
  ca = ["$CA"]
[registry."production.cloudflare.docker.com"]
  ca = ["$CA"]
EOF
docker buildx inspect ci-builder >/dev/null 2>&1 || \
  docker buildx create --name ci-builder --driver docker-container --buildkitd-config /tmp/buildkitd.toml >/dev/null
echo "== multi-arch build (as CI): linux/amd64,linux/arm64"
docker buildx build --builder ci-builder --platform linux/amd64,linux/arm64 --progress plain ./module 2>&1 \
  | grep -E '^#[0-9]+ (\[linux/[a-z0-9]+ [^]]*\]|DONE|ERROR)|ERROR|error:' | grep -vE 'CACHED' | tail -25
echo "== load linux/amd64 as datalogger-extractor:ci"
docker buildx build --builder ci-builder --platform linux/amd64 --load -t datalogger-extractor:ci ./module >/dev/null 2>&1
docker image inspect datalogger-extractor:ci --format '{{.Architecture}} {{.Size}} bytes, created {{.Created}}'
docker run --rm datalogger-extractor:ci --help | head -3
