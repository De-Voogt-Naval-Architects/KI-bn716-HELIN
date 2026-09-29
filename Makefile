# Loads .env if present so local runs pick up credentials.
ifneq (,$(wildcard .env))
include .env
export
endif

.PHONY: token register build

## Print an M2M access token for the Platform API
token:
	./scripts/get-token.sh

## One-time module registration (writes module_id back into module.yaml)
register:
	./scripts/register.sh

## Build the module container image locally
build:
	docker build -t sample-module:local ./module
