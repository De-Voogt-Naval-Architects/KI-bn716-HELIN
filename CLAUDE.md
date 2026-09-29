# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A Helin Platform module, created from the "Helin Platform Starter Pack" template. It builds a Python edge module (a Docker container) and publishes versions of it to a Helin Platform instance. At the moment `module/main.py` is still the template's sample, an imaginary OPC-UA collector.

## Commands

`make` loads `.env` if it exists; `.env.example` lists the variables. The helper scripts are bash and need `curl` and `jq` (on Windows, run them from Git Bash).

- `make build`: builds the image locally (`docker build -t sample-module:local ./module`)
- `make token`: prints an M2M access token (`scripts/get-token.sh`)
- `make register`: registers the module with the platform, once (`POST /api/v1/modules`)
- `scripts/publish-version.sh <version> <image_name> <image_tag>`: publishes a version by hand; CI normally does this
- To release: `git tag vX.Y.Z && git push origin vX.Y.Z`

There are no tests and no linter configured.

## Architecture / release flow

1. **`module.yaml`** holds the module's identity (name ≤32 chars, `meta_name` slug, author, category_id). `register.sh` reads it with `grep`/`cut`, not a YAML parser, so values must be plain single-line text with no quotes and no comments on the same line. After registration, `module_id` and `module_uuid` are appended to this file and must be committed. If `module_id` is already present, `register.sh` does nothing.
2. **`module_metadata.json`** is the body sent with every published version, merged with `version`, `image_name` and `image_tag`:
   - `container_template`: Docker create options, including the named volume bound to `/data`. The volume name must be unique to this module, otherwise modules share and overwrite each other's config.
   - `environment_template`: default environment variables.
   - `configuration_template`: initial runtime config. It must stay in sync with `DEFAULT_CONFIGURATION` in `module/main.py`.
   - `enabled`: whether the version can be deployed.
3. **CI** (`.github/workflows/`):
   - `build.yml` runs on PRs and on pushes to `main`. It only checks that the image builds for linux/amd64, arm64 and arm/v7.
   - `publish.yml` runs on `v*` tags. It builds and pushes `$REGISTRY_URL/<meta_name>:<version>` for all three architectures, then calls `publish-version.sh`. It needs six Action secrets: `HELIN_CLIENT_ID`, `HELIN_CLIENT_SECRET`, `HELIN_INSTANCE`, `REGISTRY_URL`, `REGISTRY_USER`, `REGISTRY_PASSWORD`. CI never creates modules; `module_uuid` has to be committed already.
4. **Auth** (`get-token.sh`): nothing is hardcoded. It fetches the auth domain and audience from `$HELIN_INSTANCE/api/v1/configuration/`, then runs an OAuth2 `client_credentials` grant.

## Module code (`module/`)

- The module is built on `helin-edge-sdk` (pinned `==0.0.2`, managed with Poetry). It subclasses `EdgeModuleRequestsHandler` and implements `on_get_configuration`, `on_set_configuration` and `on_get_health`. To report errors back to the platform, raise `ConfigurationFailedError(..., status_code=, current_configuration=)`.
- It starts with `EdgeModuleClient.from_edge_environment(handler).run()`. This reads the `IOTEDGE_*` environment variables that the edge runtime injects, and it blocks. Any long-running work (polling loops etc.) has to start before `.run()`.
- Config is persisted to `$CONFIG_PATH` (default `/data/configuration.json`) with an atomic write (temp file, then `os.replace`). On load, the saved values are merged over the defaults. The container filesystem is lost on redeploy; only the `/data` volume survives.
- Dockerfile: stage 1 runs `poetry export` to produce `requirements.txt`, stage 2 installs it with `pip`. It runs as a non-root `module` user (uid 1000). After changing dependencies, update `poetry.lock` (`poetry lock` in `module/`) and keep dependencies pinned.
- If `meta_name` in `module.yaml` changes, also update the image tag in the `Makefile` and the volume name in `module_metadata.json`.
