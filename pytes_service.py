import json
import os
import select
import signal
import threading
import time
import termios
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

BAUD_MAP = {
    1200: termios.B1200,
    2400: termios.B2400,
    4800: termios.B4800,
    9600: termios.B9600,
    19200: termios.B19200,
    38400: termios.B38400,
    57600: termios.B57600,
    115200: termios.B115200,
    230400: termios.B230400,
}

END_MARKER = b"Command completed successfully"

def set_raw_serial(fd: int, baud: int) -> None:
    if baud not in BAUD_MAP:
        raise ValueError(f"Baud not supported: {baud}. Try one between: {sorted(BAUD_MAP.keys())}")

    attrs = termios.tcgetattr(fd)

    attrs[0] = 0
    attrs[1] = 0

    cflag = attrs[2]
    cflag &= ~termios.CSIZE
    cflag |= termios.CS8
    cflag |= termios.CREAD | termios.CLOCAL
    cflag &= ~(termios.PARENB | termios.CSTOPB)

    if hasattr(termios, "CRTSCTS"):
        cflag &= ~termios.CRTSCTS

    attrs[2] = cflag
    attrs[3] = 0

    attrs[4] = BAUD_MAP[baud]
    attrs[5] = BAUD_MAP[baud]

    attrs[6][termios.VMIN] = 0
    attrs[6][termios.VTIME] = 1

    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    termios.tcflush(fd, termios.TCIOFLUSH)

def read_until(fd: int, marker: bytes, timeout_s: float) -> bytes:
    deadline = time.time() + timeout_s
    buf = bytearray()

    while time.time() < deadline:
        r, _, _ = select.select([fd], [], [], 0.2)
        if not r:
            continue

        chunk = os.read(fd, 4096)
        if not chunk:
            continue

        buf.extend(chunk)
        if marker in buf:
            break

    return bytes(buf)

class PytesConsole:
    def __init__(self, port: str, baud: int, timeout_s: float) -> None:
        self.port = port
        self.baud = baud
        self.timeout_s = timeout_s
        self.fd = None

    def open(self) -> None:
        fd = os.open(self.port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        set_raw_serial(fd, self.baud)
        self.fd = fd

        os.write(self.fd, b"\r")
        time.sleep(0.15)
        _ = read_until(self.fd, END_MARKER, 0.5)

    def close(self) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            finally:
                self.fd = None

    def cmd(self, command: str) -> str:
        if self.fd is None:
            raise RuntimeError("Serial not open")

        termios.tcflush(self.fd, termios.TCIOFLUSH)
        os.write(self.fd, (command + "\r").encode("ascii", errors="ignore"))

        data = read_until(self.fd, END_MARKER, self.timeout_s)
        return data.decode("utf-8", errors="ignore")

def _lines_between_table(resp: str, header_prefix: str) -> list[str]:
    lines = [ln.rstrip() for ln in resp.splitlines()]
    start = None
    for i, ln in enumerate(lines):
        if ln.strip().startswith(header_prefix):
            start = i + 1
            break
    if start is None:
        return []

    out = []
    for ln in lines[start:]:
        s = ln.strip()
        if not s:
            continue
        if "Command completed successfully" in s:
            break
        if s.startswith("PYTES"):
            continue
        if s == "@":
            continue
        out.append(s)
    return out

def parse_pwr(resp: str) -> list[dict]:
    rows = _lines_between_table(resp, "Power")
    modules: list[dict] = []

    for row in rows:
        tokens = row.split()
        if not tokens:
            continue
        if not tokens[0].isdigit():
            continue

        mid = int(tokens[0])
        if "Absent" in tokens:
            modules.append({"id": mid, "present": False})
            continue

        if len(tokens) < 19:
            modules.append({"id": mid, "present": True, "raw": row})
            continue

        volt_mv = int(tokens[1])
        curr_ma = int(tokens[2])
        tempr_mc = int(tokens[3])
        soc_percent = int(tokens[12].rstrip("%"))

        voltage_v = volt_mv / 1000.0
        current_a = curr_ma / 1000.0
        temperature_c = tempr_mc / 1000.0

        modules.append({
            "id": mid,
            "present": True,
            "voltage_v": voltage_v,
            "current_a": current_a,
            "power_w": voltage_v * current_a,
            "temperature_c": temperature_c,
            "soc_percent": soc_percent,
        })

    return modules

def parse_bat(resp: str, module_id: int) -> list[dict]:
    rows = _lines_between_table(resp, "Battery")
    cells: list[dict] = []

    for row in rows:
        tokens = row.split()
        if len(tokens) < 9:
            continue
        if not tokens[0].isdigit():
            continue

        cell = int(tokens[0])
        volt_mv = int(tokens[1])
        tempr_mc = int(tokens[2])
        capacity_mah = int(tokens[8])

        cells.append({
            "module_id": module_id,
            "cell": cell,
            "voltage_v": volt_mv / 1000.0,
            "temperature_c": tempr_mc / 1000.0,
            "capacity_mah": capacity_mah,
        })

    return cells

def merge_batteries(modules: list[dict], cells: list[dict]) -> list[dict]:
    cells_by_module: dict[int, list[dict]] = {}
    for cell in cells:
        module_id = cell["module_id"]
        cells_by_module.setdefault(module_id, []).append(cell)

    batteries: list[dict] = []
    for module in modules:
        if module.get("present") is not True:
            continue
        if "voltage_v" not in module or "current_a" not in module or "temperature_c" not in module or "soc_percent" not in module:
            continue

        module_cells = cells_by_module.get(module["id"], [])
        avg_capacity_mah = round(sum(cell["capacity_mah"] for cell in module_cells) / len(module_cells)) if module_cells else 0

        battery = {
            "module_id": module["id"],
            "voltage_v": round(module["voltage_v"], 4),
            "current_a": round(module["current_a"], 3),
            "power_w": round(module["power_w"], 3),
            "temperature_c": round(module["temperature_c"], 2),
            "soc_percent": module["soc_percent"],
            "capacity_mah": avg_capacity_mah,
        }

        for cell_idx in range(16):
            battery[f"cell{cell_idx}_v"] = None

        for cell in module_cells:
            cell_idx = cell["cell"]
            if 0 <= cell_idx < 16:
                battery[f"cell{cell_idx}_v"] = round(cell["voltage_v"], 4)

        batteries.append(battery)

    batteries.sort(key=lambda battery: battery["module_id"])
    return batteries

def validate_cells_complete(present_ids: list[int], cells: list[dict]) -> str | None:
    expected_cells = set(range(16))
    cells_by_module: dict[int, set[int]] = {module_id: set() for module_id in present_ids}

    for cell in cells:
        module_id = cell["module_id"]
        cell_idx = cell["cell"]
        if module_id not in cells_by_module:
            continue
        if 0 <= cell_idx < 16:
            cells_by_module[module_id].add(cell_idx)

    incomplete_modules: list[str] = []
    for module_id in present_ids:
        found_cells = cells_by_module.get(module_id, set())
        if found_cells != expected_cells:
            missing_cells = sorted(expected_cells - found_cells)
            incomplete_modules.append(f"{module_id} missing {missing_cells}")

    if incomplete_modules:
        return "Incomplete bat data: " + "; ".join(incomplete_modules)

    return None

def module_cells_complete(cells: list[dict]) -> bool:
    return {cell["cell"] for cell in cells if 0 <= cell["cell"] < 16} == set(range(16))

def module_data_complete(module: dict) -> bool:
    required_keys = {"id", "present", "voltage_v", "current_a", "power_w", "temperature_c", "soc_percent"}
    return required_keys.issubset(module.keys()) and module.get("present") is True

def build_summary(batteries: list[dict], cells: list[dict]) -> dict:
    present_modules = batteries

    summary = {
        "total_current_a": None,
        "total_power_w": None,
        "avg_soc_percent": None,
        "min_soc_percent": None,
        "max_soc_percent": None,
        "avg_module_voltage_v": None,
        "min_module_voltage_v": None,
        "max_module_voltage_v": None,
        "avg_module_temperature_c": None,
        "min_cell_voltage_v": None,
        "min_cell_voltage_module_id": None,
        "min_cell_voltage_cell": None,
        "max_cell_voltage_v": None,
        "max_cell_voltage_module_id": None,
        "max_cell_voltage_cell": None,
        "cell_delta_v": None,
        "avg_cell_voltage_v": None,
        "min_cell_temperature_c": None,
        "max_cell_temperature_c": None,
        "avg_cell_temperature_c": None,
    }

    if present_modules:
        total_current_a = sum(m["current_a"] for m in present_modules)
        total_power_w = sum(m["power_w"] for m in present_modules)
        soc_values = [m["soc_percent"] for m in present_modules]
        module_voltages = [m["voltage_v"] for m in present_modules]
        module_temperatures = [m["temperature_c"] for m in present_modules]

        summary["total_current_a"] = round(total_current_a, 3)
        summary["total_power_w"] = round(total_power_w, 3)
        summary["avg_soc_percent"] = round(sum(soc_values) / len(soc_values), 2)
        summary["min_soc_percent"] = min(soc_values)
        summary["max_soc_percent"] = max(soc_values)
        summary["avg_module_voltage_v"] = round(sum(module_voltages) / len(module_voltages), 4)
        summary["min_module_voltage_v"] = round(min(module_voltages), 4)
        summary["max_module_voltage_v"] = round(max(module_voltages), 4)
        summary["avg_module_temperature_c"] = round(sum(module_temperatures) / len(module_temperatures), 2)

    if cells:
        cell_voltages = [c["voltage_v"] for c in cells]
        cell_temperatures = [c["temperature_c"] for c in cells]

        min_cell = min(cells, key=lambda c: c["voltage_v"])
        max_cell = max(cells, key=lambda c: c["voltage_v"])

        summary["min_cell_voltage_v"] = round(min_cell["voltage_v"], 4)
        summary["min_cell_voltage_module_id"] = min_cell["module_id"]
        summary["min_cell_voltage_cell"] = min_cell["cell"]

        summary["max_cell_voltage_v"] = round(max_cell["voltage_v"], 4)
        summary["max_cell_voltage_module_id"] = max_cell["module_id"]
        summary["max_cell_voltage_cell"] = max_cell["cell"]

        summary["cell_delta_v"] = round(max_cell["voltage_v"] - min_cell["voltage_v"], 4)
        summary["avg_cell_voltage_v"] = round(sum(cell_voltages) / len(cell_voltages), 4)

        summary["min_cell_temperature_c"] = round(min(cell_temperatures), 2)
        summary["max_cell_temperature_c"] = round(max(cell_temperatures), 2)
        summary["avg_cell_temperature_c"] = round(sum(cell_temperatures) / len(cell_temperatures), 2)

    return summary    

class SnapshotStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshot = {
            "host_ts_iso": datetime.now(timezone.utc).isoformat(),
            "host_ts_unix_ms": int(time.time() * 1000),
            "summary": {},
            "batteries": [],
        }
        self._last_error = None

    def set(self, snapshot: dict, last_error: str | None) -> None:
        with self._lock:
            self._snapshot = snapshot
            self._last_error = last_error

    def get(self) -> tuple[dict, str | None]:
        with self._lock:
            return self._snapshot, self._last_error

class Poller(threading.Thread):
    def __init__(self, store: SnapshotStore, stop_evt: threading.Event) -> None:
        super().__init__(daemon=True)
        self.store = store
        self.stop_evt = stop_evt

        self.port = os.environ.get("SERIAL_PORT", "/dev/ttyUSB0")
        self.baud = int(os.environ.get("SERIAL_BAUD", "115200"))
        self.poll_interval = float(os.environ.get("POLL_INTERVAL_S", "30"))
        self.timeout_s = float(os.environ.get("SERIAL_TIMEOUT_S", "4"))
        self.max_modules = int(os.environ.get("MAX_MODULES", "16"))
        self.bat_retries = int(os.environ.get("BAT_RETRIES", "2"))
        self.module_cache: dict[int, dict] = {}
        self.cell_cache: dict[int, list[dict]] = {}

    def run(self) -> None:
        backoff = 1.0

        while not self.stop_evt.is_set():
            console = PytesConsole(self.port, self.baud, self.timeout_s)
            last_error = None

            try:
                console.open()
                backoff = 1.0

                while not self.stop_evt.is_set():
                    host_ts_unix_ms = int(time.time() * 1000)
                    host_ts_iso = datetime.now(timezone.utc).isoformat()

                    pwr_raw = console.cmd("pwr")
                    modules = parse_pwr(pwr_raw)

                    prev_snapshot, _ = self.store.get()
                    if not modules and prev_snapshot.get("batteries"):
                        self.store.set(prev_snapshot, "Skipped snapshot due to empty pwr response")
                        time.sleep(self.poll_interval)
                        continue

                    resolved_modules: list[dict] = []
                    reused_pwr_modules: list[int] = []
                    missing_pwr_modules: list[int] = []
                    for module in modules:
                        if module.get("present") is not True:
                            resolved_modules.append(module)
                            continue

                        if module_data_complete(module):
                            self.module_cache[module["id"]] = dict(module)
                            resolved_modules.append(module)
                            continue

                        cached_module = self.module_cache.get(module["id"])
                        if cached_module is not None and module_data_complete(cached_module):
                            resolved_modules.append(dict(cached_module))
                            reused_pwr_modules.append(module["id"])
                        else:
                            resolved_modules.append(module)
                            missing_pwr_modules.append(module["id"])

                    modules = resolved_modules

                    present_ids = [m["id"] for m in modules if m.get("present") is True]
                    present_ids = [i for i in present_ids if 1 <= i <= self.max_modules]

                    all_cells: list[dict] = []
                    reused_cache_modules: list[int] = []
                    missing_cache_modules: list[int] = []
                    for mid in present_ids:
                        module_cells: list[dict] = []
                        for _attempt in range(self.bat_retries + 1):
                            bat_raw = console.cmd(f"bat {mid}")
                            module_cells = parse_bat(bat_raw, mid)
                            if module_cells_complete(module_cells):
                                break
                            time.sleep(0.15)

                        if module_cells_complete(module_cells):
                            self.cell_cache[mid] = module_cells
                            all_cells.extend(module_cells)
                        else:
                            cached_cells = self.cell_cache.get(mid)
                            if cached_cells is not None and module_cells_complete(cached_cells):
                                all_cells.extend(cached_cells)
                                reused_cache_modules.append(mid)
                            else:
                                all_cells.extend(module_cells)
                                missing_cache_modules.append(mid)

                        time.sleep(0.1)

                    self.cell_cache = {mid: cells for mid, cells in self.cell_cache.items() if mid in present_ids}

                    batteries = merge_batteries(modules, all_cells)

                    partial_error = validate_cells_complete(present_ids, all_cells)
                    error_parts: list[str] = []
                    if reused_pwr_modules:
                        error_parts.append(f"Reused cached pwr data for modules {sorted(set(reused_pwr_modules))}")
                    if missing_pwr_modules:
                        error_parts.append(f"Incomplete pwr data for modules {sorted(set(missing_pwr_modules))}")
                    if reused_cache_modules:
                        error_parts.append(f"Reused cached cell data for modules {sorted(set(reused_cache_modules))}")
                    if missing_cache_modules and partial_error is not None:
                        error_parts.append(partial_error)
                    last_error = "; ".join(error_parts) if error_parts else None

                    if not batteries and prev_snapshot.get("batteries"):
                        keep_error = last_error or "Skipped empty snapshot due to incomplete pwr data"
                        self.store.set(prev_snapshot, keep_error)
                        time.sleep(self.poll_interval)
                        continue

                    snapshot = {
                        "host_ts_iso": host_ts_iso,
                        "host_ts_unix_ms": host_ts_unix_ms,
                        "summary": build_summary(batteries, all_cells),
                        "batteries": batteries,
                    }

                    self.store.set(snapshot, last_error)
                    time.sleep(self.poll_interval)

            except Exception as e:
                last_error = f"{type(e).__name__}: {e}"
                snap, _ = self.store.get()
                self.store.set(snap, last_error)
                console.close()

                time.sleep(backoff)
                backoff = min(backoff * 2.0, 30.0)

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

class Handler(BaseHTTPRequestHandler):
    store: SnapshotStore = None

    def _send_json(self, code: int, obj: dict) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path == "/health":
            snap, err = self.store.get()
            self._send_json(200, {"ok": True, "last_error": err, "ts": snap.get("host_ts_iso")})
            return

        if self.path == "/metrics" or self.path == "/":
            snap, err = self.store.get()
            out = dict(snap)
            out["last_error"] = err
            self._send_json(200, out)
            return

        self._send_json(404, {"error": "not found"})

    def log_message(self, fmt: str, *args) -> None:
        return

def main() -> None:
    host = os.environ.get("HTTP_HOST", "0.0.0.0")
    port = int(os.environ.get("HTTP_PORT", "8080"))

    store = SnapshotStore()
    stop_evt = threading.Event()

    Handler.store = store

    poller = Poller(store, stop_evt)
    poller.start()

    httpd = ThreadedHTTPServer((host, port), Handler)

    def _stop(*_args) -> None:
        stop_evt.set()
        try:
            httpd.shutdown()
        except Exception:
            pass

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    httpd.serve_forever()

if __name__ == "__main__":
    main()
