.PHONY: test venv clean clean-venv

# Python venv for the test suite, colocated and gitignored.  Created
# on first `make test`; reused afterward.  The host's system Python is
# never touched.  Mirrors browser-visit-sync-server/Makefile.
VENV       := .venv-test
VENV_BIN   := $(VENV)/bin
VENV_PY    := $(VENV_BIN)/python
VENV_PIP   := $(VENV_BIN)/pip
VENV_STAMP := $(VENV)/.installed

venv: $(VENV_STAMP)

$(VENV_STAMP): requirements-test.txt
	python3 -m venv $(VENV)
	$(VENV_PIP) install --upgrade pip
	$(VENV_PIP) install -r requirements-test.txt
	touch $(VENV_STAMP)

# Run the suite with coverage over every shipped module.  --cov-fail-under
# makes a coverage regression a hard failure.
test: venv
	$(VENV_PY) -m pytest tests/ \
		--cov=reading_list --cov=db_diff --cov=fetch_vm_snapshot \
		--cov=enroll_machine --cov=migrate_icloud_to_gdrive \
		--cov=_bvl_aws --cov=manage_vm --cov=manage_sync_server \
		--cov-report=term-missing --cov-fail-under=100

clean:
	rm -rf .pytest_cache .coverage

clean-venv:
	rm -rf $(VENV)
