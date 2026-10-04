# Developer shortcuts. Everything here runs without root or WiFi hardware.
PYTHON ?= python3

.PHONY: test coverage lint fmt check clean

test:
	$(PYTHON) -m unittest discover -s tests -t .

coverage:
	$(PYTHON) -m coverage run -m unittest discover -s tests -t .
	$(PYTHON) -m coverage report

lint:
	ruff check .
	ruff format --check .

fmt:
	ruff format .
	ruff check --fix .

check: lint coverage

clean:
	rm -rf build dist *.egg-info .coverage coverage.xml
	find . -name __pycache__ -prune -exec rm -rf {} +
