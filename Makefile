#
# Makefile for statement-processor.
# Runs tooling in the current directory so symlinked copies behave locally.
#

.PHONY: help rebuild-venvs update-venvs format lint type-check security vulture test test-verbose clean

# Common exclusions for Python tooling.
PY_EXCLUDES := -not -path '*/venv/*' -not -path '*/.venv/*' -not -path '*/__pycache__/*'

# Pylint/mypy settings mirrored from numerint/environments.
PYLINT_DISABLE := E0401,R0902,C0302,W0511,R0914,R0903,C0103,R0917,R0913
# Use one pylint process so lint works reliably across local and sandbox environments (agents).
PYLINT_JOBS ?= 1
PY_FIND := find . -type f -name '*.py' ! -name 'test*' $(PY_EXCLUDES)
VULTURE_CONFIDENCE ?= 90

help:
	@echo "statement-processor - Development Commands"
	@echo "=========================================="
	@echo ""
	@echo "📦 Dependency Management:"
	@printf "  %-16s %s\n" "rebuild-venv" "Rebuild venv from scratch"
	@printf "  %-16s %s\n" "update-venv" "Update dependencies in venv"
	@echo ""
	@echo "🔍 Code Quality:"
	@printf "  %-16s %s\n" "format" "Format code and sort imports with Ruff"
	@printf "  %-16s %s\n" "lint" "Run pylint in the current directory"
	@printf "  %-16s %s\n" "type-check" "Run mypy in the current directory"
	@printf "  %-16s %s\n" "vulture" "Report unused code (min confidence $(VULTURE_CONFIDENCE)%; override with VULTURE_CONFIDENCE=NN)"
	@echo ""
	@echo "🔒 Security:"
	@printf "  %-16s %s\n" "security" "Run Bandit security scanner"
	@echo ""
	@echo "🧪 Testing:"
	@printf "  %-16s %s\n" "test" "Run unit tests in ./tests (excludes Playwright)"
	@printf "  %-16s %s\n" "test-verbose" "Run unit tests in verbose mode (excludes Playwright)"
	@echo ""
	@echo "🧹 Cleanup:"
	@printf "  %-16s %s\n" "clean" "Remove Python caches and build artifacts"
	@printf "  %-16s %s\n" "dev" "Run format, lint, type-check, test, security"
	@echo ""
	@echo "Quick: make dev"

# Update or create venv and install requirements (mirrors numerint/environments).
rebuild-venv:
	@echo "🔄 Rebuilding venv..."
	@./update_dependencies.sh --rebuild
	@echo "✅ Venv rebuilt"

update-venv:
	@echo "⬆️  Updating venv dependencies..."
	@./update_dependencies.sh
	@echo "✅ All venvs updated"

# Format all code with Ruff (includes import sorting).
format:
	@echo "🎨 Formatting code and sorting imports with Ruff..."
	@bash -c "source venv/bin/activate && ruff check --select I --fix . && ruff format . 2>/dev/null || true"
	@echo "✅ Ruff formatting complete"

# Linting for the current directory.
lint:
	@echo "🔍 Running pylint in the current directory..."
	@bash -c "source venv/bin/activate && $(PY_FIND) | xargs pylint --jobs=$(PYLINT_JOBS) --disable=$(PYLINT_DISABLE) 2>/dev/null || true"

# Mypy checks for the current directory.
type-check:
	@echo "🔍 Running mypy in the current directory..."
	@bash -c "source venv/bin/activate && $(PY_FIND) | xargs mypy --ignore-missing-imports --check-untyped-defs 2>/dev/null || true"

vulture:
	@echo "🦅 Reporting unused code with Vulture (min confidence $(VULTURE_CONFIDENCE)%)..."
	@bash -c "source venv/bin/activate && vulture . --min-confidence $(VULTURE_CONFIDENCE) --exclude venv,.venv,__pycache__,.mypy_cache,.ruff_cache,.pytest_cache,playwright_tests,tests,tmp 2>/dev/null || true"

# Security scanning with Bandit.
security:
	@echo "🔒 Running security scan with Bandit..."
	@bash -c "source venv/bin/activate && $(PY_FIND) | xargs bandit -ll -q 2>/dev/null || true"

# Unit tests (non-Playwright only).
test:
	@echo "🧪 Running unit tests..."
	@bash -c "source venv/bin/activate && python3.13 -m pytest tests 2>/dev/null || true"

test-verbose:
	@echo "🧪 Running unit tests (verbose)..."
	@bash -c "source venv/bin/activate && python3.13 -m pytest -vv -s --tb=long tests || true"

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

# Development workflow
dev: format lint type-check test security
	@echo "✅ Development checks complete!"
