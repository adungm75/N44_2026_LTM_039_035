.PHONY: setup test lint dbc theory sim-baseline experiments figures demo-2d demo-carla clean-results

UV ?= uv

setup:
	$(UV) sync

test:
	$(UV) run pytest -q

lint:
	$(UV) run ruff check .
	$(UV) run ruff format --check .

# Targets below are implemented in later phases (see §6).
dbc:
	$(UV) run python -m canav.netdef.dbc --out config/dbc/canav.dbc

theory:
	$(UV) run python -m canav.timing.report --bitrate 500000 --out report/tables/theory_baseline.csv

sim-baseline:
	$(UV) run python -m canav.experiments.run --config config/experiments/E0_baseline.yaml --engine sim --seeds 1

experiments:
	@echo "experiments: not implemented yet (Phase 6)"; exit 1

figures:
	@echo "figures: not implemented yet (Phase 8)"; exit 1

demo-2d:
	@echo "demo-2d: not implemented yet (Phase 3)"; exit 1

demo-carla:
	@echo "demo-carla: not implemented yet (Phase 4)"; exit 1

clean-results:
	rm -rf results/
