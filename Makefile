.PHONY: setup test lint dbc theory sim-baseline experiments figures demo-2d demo-carla gateway-bench budget experiments-online clean-results

UV ?= uv

setup:  # extras: carla (CARLA client + networkx), parquet (pyarrow: results/summary.parquet)
	$(UV) sync --extra carla --extra parquet

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
	$(UV) run python -m canav.experiments.run --config config/experiments/E0_baseline.yaml --engine sim --seeds 1 --print

JOBS ?= 4

budget:  # AEB chain end-to-end budget (§5.12.1)
	$(UV) run python -m canav.timing.budget

experiments: budget  # all SimPy experiments (cached), summary, criteria
	$(UV) run python -m canav.experiments.run --jobs $(JOBS) --config config/experiments/E*.yaml
	$(UV) run python -m canav.experiments.summary
	$(UV) run python -m canav.analysis.criteria

experiments-online: budget  # USER: vcan0 up; sim2d E0, E7 drop 5 % / silent at Control
	$(UV) run python scripts/run_online_experiments.py
	$(UV) run python -m canav.analysis.criteria

figures:
	@echo "figures: not implemented yet (Phase 8)"; exit 1

demo-2d:
	$(UV) run python scripts/launch_stack.py --backend sim2d --iface vcan0 --duration 120

demo-carla:  # USER: CARLA 0.9.16 server running
	$(UV) run --extra carla python scripts/launch_stack.py --backend carla --iface vcan0 --duration 300

gateway-bench:  # USER: vcan0 up; ~3 min: gateway latency vs batching, monitor vs canbusload
	$(UV) run python scripts/measure_gateway.py --window 15

clean-results:
	rm -rf results/
