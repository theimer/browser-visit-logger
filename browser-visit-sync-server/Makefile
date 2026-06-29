.PHONY: proto proto-py build build-linux test cover test-integration venv clean clean-venv

PROTO_DIR  := proto
GEN_DIR    := gen/syncpb
GEN_PY_DIR := gen/syncpb_py

# Interpreter used to build the Python-stub venv below.  Override if the
# default python3 is too new for prebuilt grpcio-tools wheels (it would
# otherwise fall back to a from-source build and fail), e.g.
#   make proto-py PYTHON=python3.13
PYTHON     ?= python3
PROTO_VENV := gen/.venv-protoc
PROTO_VPY  := $(PROTO_VENV)/bin/python

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

# Generate the *Python* gRPC stubs (sync_pb2, sync_pb2_grpc) that the
# laptop sync client (browser-visit-logger/native-host/sync_client.py) and
# browser-visit-tools/fetch_vm_snapshot.py import from gen/syncpb_py/.
# Uses a dedicated, unpinned grpcio-tools venv so it picks up wheels for
# the chosen PYTHON rather than the integration suite's pinned grpcio.
proto-py: $(PROTO_VENV)/.installed
	mkdir -p $(GEN_PY_DIR)
	$(PROTO_VPY) -m grpc_tools.protoc \
		-I $(PROTO_DIR) \
		--python_out=$(GEN_PY_DIR) --grpc_python_out=$(GEN_PY_DIR) \
		$(PROTO_DIR)/sync.proto

$(PROTO_VENV)/.installed:
	$(PYTHON) -m venv $(PROTO_VENV)
	$(PROTO_VENV)/bin/pip install --upgrade pip
	$(PROTO_VENV)/bin/pip install grpcio-tools
	touch $(PROTO_VENV)/.installed

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
