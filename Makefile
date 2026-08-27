.PHONY: test lint typecheck run-chaos report clean docker-up docker-down

test:
	python scripts/run_tests.py

lint:
	python -m ruff check src scripts

typecheck:
	python -m mypy src scripts

run-chaos:
	python scripts/run_chaos.py --config configs/default.yaml --out reports/metrics.json

report:
	python scripts/generate_report.py --metrics reports/metrics.json --out reports/final_report.md

docker-up:
	docker compose up -d

docker-down:
	docker compose down

clean:
	rm -rf .pytest_cache .ruff_cache .mypy_cache reports/metrics.json reports/final_report.md
