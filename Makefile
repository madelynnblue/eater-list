# Eater 38 history pipeline — common tasks.
# `make` with no target runs the weekly job (scan + build).

PYTHON ?= python3

.PHONY: help init scan full status enrich build test test-demo serve clean weekly

help:
	@echo "make init     create the database"
	@echo "make full     first crawl: index every capture, then bisect for changes"
	@echo "make weekly   incremental scan + rebuild (what cron should call)"
	@echo "make enrich   backfill addresses / coordinates"
	@echo "make status   coverage report"
	@echo "make build    regenerate web/data and out/"
	@echo "make test     run the unit tests"
	@echo "make test-demo run the headless browser-demo checks (needs node)"
	@echo "make serve    serve web/ on http://127.0.0.1:8787"

init:
	$(PYTHON) -m e38 init

full:
	$(PYTHON) -m e38 scan --mode full
	$(PYTHON) -m e38 build

weekly:
	$(PYTHON) -m e38 scan --mode incremental
	$(PYTHON) -m e38 build

scan:
	$(PYTHON) -m e38 scan --mode incremental

enrich:
	$(PYTHON) -m e38 enrich

status:
	$(PYTHON) -m e38 status

build:
	$(PYTHON) -m e38 build

test:
	$(PYTHON) -m unittest discover -s tests -t .

test-demo:
	node tests/demo_smoke.js .

serve:
	@echo "http://127.0.0.1:8787"
	cd web && $(PYTHON) -m http.server 8787 --bind 127.0.0.1

clean:
	rm -rf __pycache__ e38/__pycache__ tests/__pycache__
