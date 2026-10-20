.PHONY: test

PYTHON ?= python

# Keep every test isolated from the caller's application database.
test: override export CAMPUSRELAY_DB := :memory:
test:
	$(PYTHON) -B -m unittest discover -s tests -p "test_*.py" -v
