.PHONY: compile hooks-install lint test build

compile:
	pip-compile requirements.in -o requirements.txt --generate-hashes --strip-extras

hooks-install:
	pre-commit install && pre-commit install --hook-type commit-msg

lint:
	pre-commit run --all-files

test:
	python -m pytest tests/

build:
	docker build -t oci-free-tier-monitor .
