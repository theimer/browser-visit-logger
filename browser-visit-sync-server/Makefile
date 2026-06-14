.PHONY: proto build build-linux test cover test-integration venv clean clean-venv

PROTO_DIR := proto
GEN_DIR   := gen/syncpb

# Python venv for the integration suite.  Lives inside test/ so it's
# colocated with what uses it; gitignored.  Auto-created on first
# `make test-integration` so the user doesn't need to remember a
# separate setup step.
VENV       := test/.venv
VENV_BIN   := $(VENV)/bin
VENV_PY    := $(VENV_BIN)/python
VENV_PIP   := $(VENV_BIN)/pip
VENV_STAMP := $(VENV)/.installed

proto:
	mkdir -p $(GEN_DIR)
	protoc \
		-I $(PROTO_DIR) \
		--go_out=$(GEN_DIR) --go_opt=paths=source_relative \
		--go-grpc_out=$(GEN_DIR) --go-grpc_opt=paths=source_relative \
		$(PROTO_DIR)/sync.proto

build:
	go build -o bin/sync-server ./cmd/sync-server

# Cross-compile a stripped Linux binary for the EC2 VM.  CGO_ENABLED=0 is
# safe because the SQLite driver (modernc.org/sqlite) is pure Go.  GOARCH
# must match the instance arch chosen by manage_vm.py (amd64↔x86_64,
# arm64↔arm64).  Consumed by ../browser-visit-tools/manage_sync_server.py.
GOARCH ?= amd64
build-linux:
	CGO_ENABLED=0 GOOS=linux GOARCH=$(GOARCH) \
		go build -trimpath -ldflags="-s -w" \
		-o bin/sync-server-linux-$(GOARCH) ./cmd/sync-server

test:
	go test ./...

# Per-package coverage over the hand-written packages.  Generated
# protobuf bindings (gen/syncpb) are excluded by convention.
cover:
	go test $$(go list ./... | grep -v '/gen/syncpb') -cover

# Idempotent: re-runs are fast (pip notices everything's already up
# to date).  Rebuild the venv after editing test/requirements.txt by
# running `make clean-venv test-integration`, or `make venv` alone.
venv: $(VENV_STAMP)

$(VENV_STAMP): test/requirements.txt
	python3 -m venv $(VENV)
	$(VENV_PIP) install --upgrade pip
	$(VENV_PIP) install -r test/requirements.txt
	touch $(VENV_STAMP)

test-integration: venv
	$(VENV_PY) -m pytest -v test

clean:
	rm -rf bin gen test/certs test/state

clean-venv:
	rm -rf $(VENV)
