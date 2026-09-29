# Helin Platform Starter Pack

A GitHub **template repository** for building and publishing modules to your Helin Platform instance. Create your own repo from this template, fill in the secrets, and go from zero to a published module version in minutes.

## What's inside

```
.
├── scripts/              # helper scripts (M2M token, registration, publishing)
│   ├── get-token.sh      # fetch an M2M access token via client_credentials
│   ├── register.sh       # one-time module registration (POST /modules)
│   └── publish-version.sh # publish a module version (used by CI)
├── module/               # your module code (sample built on helin-edge-sdk)
│   ├── main.py
│   ├── Dockerfile
│   ├── pyproject.toml
│   └── poetry.lock
├── .github/workflows/
│   └── publish.yml       # on tag v*: build, push image, publish module version
├── module.yaml           # module metadata; module_id/module_uuid written here after registration
├── module_metadata.json  # deploy-time templates sent with every published version
├── Makefile
├── .env.example          # placeholders for local development
└── README.md
```

## Quickstart

1. **Create your repo** - click **"Use this template"** on GitHub, then set the six [required secrets](#required-secrets) under *Settings > Secrets and variables > Actions*.
2. **Register your module (once)** - edit `module.yaml` (name, meta_name, author, category_id), then run `make register`. The returned `module_id` and `module_uuid` are written back into `module.yaml`; commit them.
3. **Publish a version** - push a git tag, e.g. `git tag v1.0.0 && git push origin v1.0.0`. CI builds the container, pushes it to your instance's private registry, and publishes version `1.0.0` of your module.

Once published, the module version can be deployed to a node from the portal.

## Required secrets

Set these as GitHub **Action secrets** on your repository:

| Secret | Description | Example |
| --- | --- | --- |
| `HELIN_CLIENT_ID` | M2M client ID for your platform instance | `A1b2C3d4E5f6...` |
| `HELIN_CLIENT_SECRET` | M2M client secret | `(provided with your credentials)` |
| `HELIN_INSTANCE` | Base URL of your platform instance (no trailing slash) | `https://your-instance.helinplatform.com` |
| `REGISTRY_URL` | URL of your instance's dedicated private container registry | `your-instance.azurecr.io` |
| `REGISTRY_USER` | Registry username | `your-instance` |
| `REGISTRY_PASSWORD` | Registry password | `(provided with your credentials)` |

For local development, copy `.env.example` to `.env` and fill in the values.

## What module_metadata.json is for

`module_metadata.json` is used for holding the deploy-time configuration:

- `container_template` - Docker create options (port bindings, volume binds). The sample mounts a named volume at `/data` so the module's persisted configuration survives redeploys. Give the volume a **name unique to your module** - two modules created from this template that keep the same name would share one volume and overwrite each other's configuration.
- `environment_template` - default environment variables for the container.
- `configuration_template` - the initial runtime configuration; this is what the portal shows and what your `on_get/set_configuration` handlers exchange. The sample prefills it with the defaults from `module/main.py`.
- `enabled` - whether the version can be deployed.

Adjust it whenever your module needs different ports, volumes, env vars, or configuration fields.

## How authentication works

Nothing is hardcoded: `scripts/get-token.sh` reads the auth domain and audience from your instance's `/api/v1/configuration/` endpoint, then runs the OAuth2 `client_credentials` grant with `HELIN_CLIENT_ID`/`HELIN_CLIENT_SECRET` and prints an access token.

## Building your own module

The sample in `module/` implements the `helin-edge-sdk` contract (`EdgeModuleRequestsHandler` with `on_get_configuration`, `on_set_configuration`, `on_get_health`) and runs via `EdgeModuleClient.from_edge_environment().run()`. Replace the sample logic with your own - keep the handler contract and the Dockerfile pattern (pinned dependencies, non-root user).

A few things the platform handles for you, so you don't have to:

- **Connection setup** - `from_edge_environment()` reads the `IOTEDGE_*` environment variables that the platform's edge runtime injects into the container. No broker addresses or credentials in your code or config.
- **Configuration persistence** - anything in the container's own filesystem is lost when the container is recreated, so the sample writes its configuration to `$CONFIG_PATH` (default `/data/configuration.json`). The module's `container_template` mounts a named volume at `/data`, which makes the file survive redeploys. Keep this pattern for any state your module can't afford to lose. To inspect the data on a node, use `docker volume inspect <volume-name>`.

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `... is not set` | A required secret (CI) or `.env` value (local) is missing. |
| Token request fails with 401 | Wrong `HELIN_CLIENT_ID` / `HELIN_CLIENT_SECRET`. |
| `register` says `already has a module_id` | The module is already registered. To re-register, remove the `module_id`/`module_uuid` lines from `module.yaml`. |
| `publish-version` says `no module_uuid` | Run `make register` once and commit `module.yaml` before publishing. |
| CI fails at "Fail fast ... secret is missing" | Add the named secret under *Settings > Secrets and variables > Actions*. |
