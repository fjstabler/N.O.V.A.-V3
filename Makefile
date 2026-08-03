.PHONY: help setup setup-core setup-desktop dev dev-core dev-desktop test test-core test-desktop \
        lint lint-core lint-desktop typecheck check build models clean

PYTHON ?= python3
VENV   := .venv
PY     := $(VENV)/bin/python
PIP    := $(VENV)/bin/pip
CORE   := services/core
DESKTOP:= apps/desktop

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# ------------------------------------------------------------------- setup

setup: setup-core setup-desktop ## Install everything

setup-core: ## Create the venv and install the core (with all extras)
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -e "$(CORE)[dev,ai,embeddings,home,server]"
	@echo "\nVoice is separate: it pulls large ML wheels."
	@echo "  $(PIP) install -e '$(CORE)[voice]'        # mic, Whisper, Kokoro"
	@echo "  $(PIP) install -e '$(CORE)[wake]'         # wake word (needs Python <= 3.13)"

setup-desktop: ## Install the desktop shell's dependencies
	cd $(DESKTOP) && npm install

models: ## Download the local voice models
	$(PY) scripts/fetch_models.py

# --------------------------------------------------------------------- dev

dev: ## Run the shell (which starts the core itself)
	cd $(DESKTOP) && npm run dev

dev-core: ## Run the core service alone, with debug logging
	$(PY) -m nova --log-level DEBUG

dev-desktop: ## Run only the shell, attaching to an already-running core
	cd $(DESKTOP) && npm run dev

# ------------------------------------------------------------------- checks

test: test-core test-desktop ## Run every test

test-core:
	$(VENV)/bin/pytest $(CORE)/tests -q

test-desktop:
	cd $(DESKTOP) && npm test

lint: lint-core lint-desktop ## Lint everything

lint-core:
	$(VENV)/bin/ruff check $(CORE)/nova $(CORE)/tests
	$(VENV)/bin/ruff format --check $(CORE)/nova $(CORE)/tests

lint-desktop:
	cd $(DESKTOP) && npm run lint

typecheck: ## Type-check both sides
	$(VENV)/bin/mypy $(CORE)/nova
	cd $(DESKTOP) && npm run typecheck

protocol: ## Fail if the Python and TypeScript protocols have drifted
	$(PYTHON) scripts/check_protocol.py

check: protocol lint test ## What CI runs

# ------------------------------------------------------------------- build

build: ## Build the production shell bundle
	cd $(DESKTOP) && npm run build

package: ## Build an installable desktop application
	cd $(DESKTOP) && npm run package

clean:
	rm -rf $(DESKTOP)/dist $(DESKTOP)/dist-electron $(DESKTOP)/release
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .ruff_cache .mypy_cache .pytest_cache
