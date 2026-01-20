#
# Root Makefile for statement-processor.
# Keeps lint/type-check commands centralized for service and lambda_functions.
#

.PHONY: help rebuild-venvs update-venvs format-all lint-all type-check-all clean

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
	@echo "  rebuild-venvs           Rebuild all venvs from scratch"
	@echo "  update-venvs            Update dependencies in each service/lambda venv"
	@echo ""
	@echo "🔍 Code Quality:"
	@echo "  format-all              Format code and sort imports with Ruff"
	@echo "  lint-all                Run pylint on all Lambdas and service (sequential, clearer output)"
	@echo "  type-check-all          Run mypy on all Lambdas and service (sequential, clearer output)"
	@echo ""
	@echo "🧹 Cleanup:"
	@echo "  clean                   Remove Python caches and build artifacts"

# Update or create venvs and install requirements (mirrors numerint/environments).
rebuild-venvs:
	@echo "🔄 Rebuilding all venvs..."
	@./update_dependencies.sh --rebuild
	@echo "✅ All venvs rebuilt"

update-venvs:
	@echo "⬆️  Updating venv dependencies..."
	@./update_dependencies.sh
	@echo "✅ All venvs updated"

# Format all code with Ruff (includes import sorting).
format-all:
	@echo "🎨 Formatting code and sorting imports with Ruff..."
	@for dir in $(SERVICE_DIR) $(LAMBDA_DIRS); do \
		if [ -d "$$dir" ]; then \
			echo "Formatting: $$dir"; \
			bash -c "cd $$dir && source venv/bin/activate && ruff check --select I --fix . && ruff format . 2>/dev/null || true"; \
		fi; \
	done
	@echo "✅ Ruff formatting complete"

# Sequential linting for service + each lambda directory.
lint-all:
	@echo "🔍 Running pylint on service and lambdas (sequential)..."
	@for dir in $(SERVICE_DIR) $(LAMBDA_DIRS); do \
		if [ -d "$$dir" ]; then \
			echo "----------------------------------------------"; \
			echo "Linting: $$dir"; \
			echo "----------------------------------------------"; \
			bash -c "cd $$dir && source venv/bin/activate && $(PY_FIND) | xargs pylint --disable=$(PYLINT_DISABLE) 2>/dev/null || true"; \
		fi; \
	done

# Sequential mypy checks for service + each lambda directory.
type-check-all:
	@echo "🔍 Running mypy on service and lambdas (sequential)..."
	@for dir in $(SERVICE_DIR) $(LAMBDA_DIRS); do \
		if [ -d "$$dir" ]; then \
			echo "----------------------------------------------"; \
			echo "Type checking: $$dir"; \
			echo "----------------------------------------------"; \
			bash -c "cd $$dir && source venv/bin/activate && $(PY_FIND) | xargs mypy --ignore-missing-imports --check-untyped-defs 2>/dev/null || true"; \
		fi; \
	done

# Clean common Python caches and generated files.
clean:
	@echo "🧹 Cleaning generated files..."
	find . -type f -name "*.pyc" -delete
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".mypy_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".ruff_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.zip" -delete
	@echo "✅ Cleanup complete"
