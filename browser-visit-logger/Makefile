.PHONY: test test-py test-js test-swift venv swift clean clean-venv

# Python venv for the pytest suite, colocated and gitignored.  Created
# on first `make test-py`; reused afterward.  The host's system Python
# is never touched.  Mirrors browser-visit-sync-server/Makefile.
VENV       := .venv-test
VENV_BIN   := $(VENV)/bin
VENV_PY    := $(VENV_BIN)/python
VENV_PIP   := $(VENV_BIN)/pip
VENV_STAMP := $(VENV)/.installed

# Run everything: Python + JS.  (Swift is built separately via `make
# swift`; there's no XCTest target — see swift/Package.swift.)
test: test-py test-js

venv: $(VENV_STAMP)

$(VENV_STAMP): requirements-test.txt
	python3 -m venv $(VENV)
	$(VENV_PIP) install --upgrade pip
	$(VENV_PIP) install -r requirements-test.txt pytest-cov
	touch $(VENV_STAMP)

# Python suite with coverage over every native-host module, including
# the multi-laptop sync_client.  --cov-fail-under makes a regression a
# hard failure.
test-py: venv
	$(VENV_PY) -m pytest tests/ \
		--cov=native-host \
		--cov-report=term-missing --cov-fail-under=100

# JS suite (extension service worker + popup) with coverage.
test-js:
	npm install
	npm test -- --coverage

# Build the Swift binaries (release).  Required before the shell
# wrappers (verify_snapshot_directory etc.) and the Swift-dependent
# Python tests will run.
swift:
	cd swift && swift build -c release

clean:
	rm -rf .pytest_cache .coverage coverage

clean-venv:
	rm -rf $(VENV)
