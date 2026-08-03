.PHONY: help
help: ## Display this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-30s\033[0m %s\n", $$1, $$2}'

.PHONY: all
all: help

# override with your preferred way, e.g. "uv run" or "poetry run"
PYTHON_RUN ?= uv run

# Detect if pytest-xdist is installed for parallel testing
PYTEST_XDIST := $(shell python3 -c "import xdist" 2>/dev/null && echo "-n auto" || echo "")
SOURCE_FILES = obs_automation tests

.PHONY: test-unit
test-unit: ## Run dynamic tests with coverage report
	$(PYTHON_RUN) python3 -m pytest $(PYTEST_XDIST) --cov --cov-report=xml --cov-report=term-missing

.PHONY: test-unit-no-coverage
test-unit-no-coverage: ## Run dynamic tests without coverage analysis and without style checks
	$(PYTHON_RUN) python3 -m pytest $(PYTEST_XDIST)

.PHONY: only-test-with-coverage
only-test-with-coverage: test-unit  ## Alias for "test-unit"

.PHONY: check-ruff
check-ruff: ## Run ruff linting and formatting checks
	$(PYTHON_RUN) ruff check $(SOURCE_FILES)
	$(PYTHON_RUN) ruff format --check $(SOURCE_FILES)

.PHONY: tidy
tidy: ## Format code and fix linting issues
	$(PYTHON_RUN) ruff format
	$(PYTHON_RUN) ruff check $(SOURCE_FILES) --fix

.PHONY: check-code-health
check-code-health: ## Find dead code (vulture)
	@echo "Checking code health…"
	$(PYTHON_RUN) vulture ${SOURCE_FILES} --min-confidence 80

.PHONY: check-types-ty
check-types-ty: ## Run ty type checker
	$(PYTHON_RUN) ty check $(SOURCE_FILES)

.PHONY: check-types
check-types: check-types-ty

.PHONY: check-lock
check-lock: ## Verify lock file is in sync
	uv lock --check

.PHONY: checkstyle
checkstyle: check-ruff check-code-health check-types ## Run full linting (ruff, vulture, ty)

.PHONY: test
test: checkstyle test-unit ## Run all tests with full coverage
