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
	@echo "dbc: not implemented yet (Phase 1)"; exit 1

theory:
	@echo "theory: not implemented yet (Phase 1)"; exit 1

sim-baseline:
	@echo "sim-baseline: not implemented yet (Phase 2)"; exit 1

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
