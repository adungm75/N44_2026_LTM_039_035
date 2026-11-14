.PHONY: setup test lint dbc theory sim-baseline experiments figures demo-2d demo-carla gateway-bench budget experiments-online clean-results

UV ?= uv

setup:  # extras: carla (CARLA client + networkx), parquet (pyarrow: results/summary.parquet)
	$(UV) sync --extra carla --extra parquet

test:
	$(UV) run pytest -q

lint:
	$(UV) run ruff check .
	$(UV) run ruff format --check .

# Targets below are implemented in later phases.
dbc:
	$(UV) run python -m canav.netdef.dbc --out config/dbc/canav.dbc

theory:
	$(UV) run python -m canav.timing.report --bitrate 500000 --out report/tables/theory_baseline.csv

sim-baseline:
	$(UV) run python -m canav.experiments.run --config config/experiments/E0_baseline.yaml --engine sim --seeds 1 --print

JOBS ?= 4

budget:  # AEB chain end-to-end budget
	$(UV) run python -m canav.timing.budget

experiments: theory budget  # all SimPy experiments incl. E8 (cached), summary, E9 detector, criteria
	$(UV) run python -m canav.experiments.run --jobs $(JOBS) --config config/experiments/E*.yaml
	$(UV) run python -m canav.experiments.summary
	$(UV) run python -m canav.analysis.detector
	$(UV) run python -m canav.analysis.criteria

experiments-online: budget  # requires: vcan0 up; sim2d E0, E7 drop/silent at Control, E8 anomalies
	$(UV) run python scripts/run_online_experiments.py
	$(UV) run python scripts/run_e8_online.py
	$(UV) run python -m canav.analysis.detector
	$(UV) run python -m canav.analysis.criteria

figures:  # report/figures/*.png + docs/results.md (after make experiments)
	$(UV) run python -m canav.analysis.figures
	$(UV) run python -m canav.analysis.results_doc

demo-2d:
	$(UV) run python scripts/launch_stack.py --backend sim2d --iface vcan0 --duration 120

demo-carla:  # requires: CARLA 0.9.16 server running
	$(UV) run --extra carla python scripts/launch_stack.py --backend carla --iface vcan0 --duration 300

gateway-bench:  # requires: vcan0 up; ~3 min: gateway latency vs batching, monitor vs canbusload
	$(UV) run python scripts/measure_gateway.py --window 15

clean-results:
	rm -rf results/
