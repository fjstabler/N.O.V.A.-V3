.PHONY: help setup setup-core setup-desktop wake models dev dev-core dev-desktop test test-core test-desktop \
        lint lint-core lint-desktop typecheck protocol check build package clean

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

# openWakeWord declares tflite-runtime on Linux but never uses it when driven
# through ONNX, which is how N.O.V.A. drives it. The declaration alone blocks
# installation on any Python without a tflite wheel — 3.14 today. Installing it
# without its dependency list, then adding what it genuinely imports, sidesteps
# a constraint that has no runtime meaning here.
wake: ## Install the wake word engine (works on Python versions tflite does not support)
	$(PIP) install --no-deps openwakeword
	$(PIP) install onnxruntime numpy scipy scikit-learn requests tqdm
	$(PY) -c "import openwakeword.utils as u; u.download_models()"
	@echo "\nWake word installed. Bundled phrases: alexa, hey_jarvis, hey_mycroft, hey_rhasspy."
	@echo "'hey nova' needs a trained model — see docs/SETUP.md."

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
