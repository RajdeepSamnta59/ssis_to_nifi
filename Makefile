# SSIS2NIFI -- every operation. `make help` lists them.
#
# Tests run in Docker because this machine has no pip or python3-venv, and
# because a tool meant to ship should have a reproducible test environment
# rather than one that depends on what happens to be installed.

PY      ?= python3
PKG     ?= python:3.12-slim
FILE    ?= corpus/packages/L1.dtsx
CORPUS  := corpus/packages

.DEFAULT_GOAL := help

.PHONY: help analyze corpus test json clean

help:  ## list these targets
	@grep -hE '^[a-z-]+:.*?##' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

analyze:  ## report one package: make analyze FILE=corpus/packages/L4.dtsx
	@$(PY) -m ssis2nifi analyze $(FILE)

json:  ## emit the IR for one package
	@$(PY) -m ssis2nifi analyze $(FILE) --json

corpus:  ## run every package and show its exit code
	@printf "%-34s %-6s %s\n" PACKAGE EXIT RESULT
	@for f in $(CORPUS)/*.dtsx; do \
	  out=$$($(PY) -m ssis2nifi analyze "$$f" --no-graph 2>&1); code=$$?; \
	  printf "%-34s %-6s %s\n" "$$(basename $$f)" "$$code" \
	    "$$(printf '%s' "$$out" | grep -E '^(coverage|refused)' | head -1 | cut -c1-60)"; \
	done

test:  ## run the suite in Docker
	@docker run --rm -v "$$PWD":/w -w /w $(PKG) \
	  sh -c "pip install -q pytest 2>/dev/null && python -m pytest tests/ -q"

clean:  ## remove generated output and caches
	@rm -rf out/* .pytest_cache
	@find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
