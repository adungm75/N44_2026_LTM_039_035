"""Online stack: bridge + ECUs, either as processes on SocketCAN or as threads on python-can
``virtual`` (in-process only). Used by ``scripts/launch_stack.py`` and the integration tests.

Run directory layout::

    config.yaml  scenario.yaml  ecu_params.yaml  route.json
    can.log (candump -L)  bridge.jsonl  driving.json  ecu/<ECU>.jsonl  ecu/<ECU>.stats.json
    proc/<name>.log (process mode: stdout/stderr)  metrics.json
"""

from __future__ import annotations

import copy
import json
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import yaml

from canav.analysis.candump import format_line
from canav.ecu import FaultConfig, build_ecu, load_dbc, make_bus
from canav.ecu.nodes import ECUS
from canav.netdef import DEFAULT_MATRIX, load_matrix
from canav.provenance import git_hash
from canav.vehicle.bridge import Bridge, make_backend

DEFAULT_SCENARIO = Path("config/scenarios/sim2d_default.yaml")
DEFAULT_PARAMS = Path("config/online/ecu_params.yaml")
DEFAULT_DBC = Path("config/dbc/canav.dbc")
DEFAULTS = {  # backend -> (scenario, ECU params)
    "sim2d": (DEFAULT_SCENARIO, DEFAULT_PARAMS),
    "carla": (
        Path("config/scenarios/carla_town02.yaml"),
        Path("config/online/ecu_params_carla.yaml"),
    ),
}
BRIDGE_START_TIMEOUT_S = 120  # CARLA may load the map and spawn actors first
ONLINE_ECUS = tuple(ECUS)  # Gateway node: raw-socket gateway in Phase 5


def _load(x: dict | str | Path) -> dict:
    return copy.deepcopy(x) if isinstance(x, dict) else yaml.safe_load(Path(x).read_text())


def _sniffer(channel: str, path: Path, iface_name: str, stop: threading.Event) -> None:
    bus = make_bus("virtual", channel)
    try:
        with open(path, "w") as f:
            while not stop.is_set():
                m = bus.recv(timeout=0.05)
                if m is not None and not m.is_error_frame:
                    t_ns = round(m.timestamp * 1e9)
                    f.write(
                        format_line(
                            t_ns, iface_name, m.arbitration_id, bytes(m.data), m.is_extended_id
                        )
                        + "\n"
                    )
    finally:
        bus.shutdown()


def _run_threads(out, backend, scenario, params, dbc, duration, faults, stop_on_goal, stop):
    db = load_dbc(dbc)
    nodes = [n.name for n in db.nodes]
    channel = f"canav-{uuid.uuid4().hex[:8]}"
    bridge = Bridge(
        make_backend(backend, scenario),
        scenario,
        out,
        nodes,
        params["sidechannel"]["port_base"],
        duration,
        stop_on_goal,
    )
    ecus = [
        build_ecu(
            name,
            "virtual",
            db,
            params,
            out / "ecu",
            channel=channel,
            fault=faults.get(name),
            seed=i,
        )
        for i, name in enumerate(ONLINE_ECUS)
    ]
    threads = [
        threading.Thread(
            target=_sniffer,
            args=(channel, out / "can.log", "vcan0", stop),
            name="sniffer",
            daemon=True,
        )
    ]
    threads += [
        threading.Thread(target=e.run, args=(stop,), name=e.name, daemon=True) for e in ecus
    ]
    for t in threads:
        t.start()
    try:
        bridge.run(stop)
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=5)
        for e in ecus:
            e.bus.shutdown()


def _spawn(cmd: list[str], log: Path, stdout=None) -> subprocess.Popen:
    err = open(log, "w")  # noqa: SIM115 - owned by the child
    return subprocess.Popen(cmd, stdout=stdout or err, stderr=err, start_new_session=True)


def _run_processes(out, backend, iface, dbc, duration, faults, stop_on_goal, stop):
    py = sys.executable
    proc_dir = out / "proc"
    proc_dir.mkdir(exist_ok=True)
    children: list[tuple[str, subprocess.Popen]] = []
    if shutil.which("candump"):
        log = open(out / "can.log", "w")  # noqa: SIM115
        children.append(
            ("candump", _spawn(["candump", "-L", iface], proc_dir / "candump.log", stdout=log))
        )
    else:
        print("candump not found: no can.log", file=sys.stderr)
    bridge_cmd = [
        py,
        "-m",
        "canav.vehicle.bridge",
        "--backend",
        backend,
        "--scenario",
        str(out / "scenario.yaml"),
        "--params",
        str(out / "ecu_params.yaml"),
        "--dbc",
        str(dbc),
        "--out",
        str(out),
        "--duration",
        str(duration),
    ]
    if not stop_on_goal:
        bridge_cmd.append("--no-stop-on-goal")
    bridge = _spawn(bridge_cmd, proc_dir / "bridge.log")
    children.append(("bridge", bridge))
    t0 = time.monotonic()
    while not (out / "route.json").exists():  # Planning loads the route at start-up
        if bridge.poll() is not None or time.monotonic() - t0 > BRIDGE_START_TIMEOUT_S:
            raise RuntimeError(f"bridge failed to start, see {proc_dir / 'bridge.log'}")
        time.sleep(0.05)
    for i, name in enumerate(ONLINE_ECUS):
        cmd = [
            py,
            "-m",
            "canav.ecu.run",
            "--ecu",
            name,
            "--iface",
            iface,
            "--dbc",
            str(dbc),
            "--params",
            str(out / "ecu_params.yaml"),
            "--log-dir",
            str(out / "ecu"),
            "--seed",
            str(i),
            "--fault",
            _fault_arg(faults.get(name)),
        ]
        children.append((name, _spawn(cmd, proc_dir / f"{name}.log")))

    previous = signal.signal(signal.SIGINT, lambda *_: stop.set())
    try:
        while not stop.is_set() and bridge.poll() is None:
            time.sleep(0.1)
            for name, p in children:
                if p.poll() not in (None, 0) and name != "bridge":
                    raise RuntimeError(f"{name} exited with {p.returncode}, see proc/{name}.log")
    finally:
        signal.signal(signal.SIGINT, previous)
        for _, p in reversed(children):  # ECUs and candump first, bridge last
            if p.poll() is None:
                p.send_signal(signal.SIGINT)
        deadline = time.monotonic() + 5
        for _, p in children:
            try:
                p.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                p.kill()


def _fault_arg(f: FaultConfig | None) -> str:
    if f is None:
        return ""
    parts = [f"drop_prob={f.drop_prob}", f"extra_delay_ms={f.extra_delay_ms}"]
    if f.silent_after_s is not None:
        parts.append(f"silent_after_s={f.silent_after_s}")
    return ",".join(parts)


def run_stack(
    out_dir: str | Path,
    *,
    backend: str = "sim2d",
    iface: str = "virtual",
    duration: float = 120.0,
    scenario: dict | str | Path = DEFAULT_SCENARIO,
    params: dict | str | Path = DEFAULT_PARAMS,
    dbc: str | Path = DEFAULT_DBC,
    matrix: str | Path = DEFAULT_MATRIX,
    faults: dict[str, FaultConfig] | None = None,
    stop_on_goal: bool = True,
    port_base: int | None = None,
    stop: threading.Event | None = None,
) -> dict:
    """Run the closed loop and return ``metrics.json`` content (also written to ``out_dir``)."""
    out = Path(out_dir)
    (out / "ecu").mkdir(parents=True, exist_ok=True)
    faults = faults or {}
    sc, prm = _load(scenario), _load(params)
    if port_base is not None:
        prm["sidechannel"]["port_base"] = port_base
    prm["route"] = str(out / "route.json")
    (out / "scenario.yaml").write_text(yaml.safe_dump(sc, sort_keys=False))
    (out / "ecu_params.yaml").write_text(yaml.safe_dump(prm, sort_keys=False))
    meta = {
        "engine": "online",
        "backend": backend,
        "iface": iface,
        "duration": duration,
        "stop_on_goal": stop_on_goal,
        "dbc": str(dbc),
        "ecus": list(ONLINE_ECUS),
        "faults": {k: _fault_arg(v) for k, v in faults.items()},
        "git": git_hash(),
        "python": sys.version.split()[0],
    }
    (out / "config.yaml").write_text(yaml.safe_dump(meta, sort_keys=False))

    stop = stop or threading.Event()
    t0 = time.monotonic()
    if iface == "virtual":
        _run_threads(out, backend, sc, prm, dbc, duration, faults, stop_on_goal, stop)
    else:
        _run_processes(out, backend, iface, dbc, duration, faults, stop_on_goal, stop)
    wall = time.monotonic() - t0

    # pandas-based analysis is imported only now, so the launcher stays small during the run
    from canav.analysis.online import compute_online_metrics

    bitrate = next(iter(load_matrix(matrix).buses.values()))  # the DBC carries no bitrate
    metrics = compute_online_metrics(out, load_dbc(dbc), bitrate)
    metrics["wall_s"] = wall
    (out / "metrics.json").write_text(json.dumps(metrics, indent=2))
    return metrics
