#
# Root Makefile for statement-processor.
# Keeps lint/type-check commands centralized for service and lambda_functions.
#

.PHONY: help update-venvs lint-all type-check-all

# Top-level directories to target.
SERVICE_DIR := service
LAMBDA_ROOT := lambda_functions
LAMBDA_DIRS := $(wildcard $(LAMBDA_ROOT)/*)

# Common exclusions for Python tooling.
PY_EXCLUDES := -not -path '*/venv/*' -not -path '*/.venv/*' -not -path '*/__pycache__/*'

# Pylint/mypy settings mirrored from numerint/environments.
PYLINT_DISABLE := E0401,R0902,C0302,W0511,R0914,R0903,C0103,R0917,R0913
PY_FIND := find . -type f -name '*.py' ! -name 'test*' $(PY_EXCLUDES)

help:
	@echo "statement-processor - Development Commands"
	@echo "=========================================="
	@echo ""
	@echo "📦 Dependency Management:"
	@echo "  update-venvs            Update dependencies in each service/lambda venv"
	@echo ""
	@echo "🔍 Code Quality:"
	@echo "  lint-all                Run pylint on all Lambdas and service (sequential, clearer output)"
	@echo "  type-check-all          Run mypy on all Lambdas and service (sequential, clearer output)"

# Update or create venvs and install requirements (mirrors numerint/environments).
update-venvs:
	@echo "⬆️  Updating venv dependencies..."
	@./update_dependencies.sh
	@echo "✅ All venvs updated"

# Sequential linting for service + each lambda directory.
lint-all:
	@echo "🔍 Running pylint on service and lambdas (sequential)..."
	@for dir in $(SERVICE_DIR) $(LAMBDA_DIRS); do \
		if [ -d "$$dir" ]; then \
			echo "Linting: $$dir"; \
			bash -c "cd $$dir && source venv/bin/activate && $(PY_FIND) | xargs pylint --disable=$(PYLINT_DISABLE) 2>/dev/null || true"; \
		fi; \
	done

# Sequential mypy checks for service + each lambda directory.
type-check-all:
	@echo "🔍 Running mypy on service and lambdas (sequential)..."
	@for dir in $(SERVICE_DIR) $(LAMBDA_DIRS); do \
		if [ -d "$$dir" ]; then \
			echo "Type checking: $$dir"; \
			bash -c "cd $$dir && source venv/bin/activate && $(PY_FIND) | xargs mypy --ignore-missing-imports --check-untyped-defs 2>/dev/null || true"; \
		fi; \
	done
