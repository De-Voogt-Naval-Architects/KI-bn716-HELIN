# Loads .env if present so local runs pick up credentials.
ifneq (,$(wildcard .env))
include .env
export
endif

.PHONY: token register build test proto

## Print an M2M access token for the Platform API
token:
	./scripts/get-token.sh

## One-time module registration (writes module_id back into module.yaml)
register:
	./scripts/register.sh

## Build the module container image locally
build:
	docker build -t datalogger-extractor:local ./module

## Run the tests (needs `poetry install --with dev --no-root` in module/)
test:
	cd module && poetry run pytest

## Regenerate the gRPC stubs after changing Trending.proto
proto:
	cd module && poetry run python -m grpc_tools.protoc -I. --python_out=. --grpc_python_out=. Trending.proto
