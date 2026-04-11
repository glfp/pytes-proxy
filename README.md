Pytes Batteries - Proxy - generate a JSON with cell infos good for telegraf/influxdb or other ingestion

A small Docker-friendly HTTP bridge for **Pytes E-BOX and v5** batteries that reads the battery console over **RS232 serial**, runs the `pwr` and `bat N` CLI commands, and exposes the result as compact **JSON** for Telegraf, InfluxDB v2, Grafana, or any other consumer.

This project was built with a very pragmatic goal: get reliable battery data out of the Pytes console without depending on vendor software, heavyweight frameworks, or fragile host-side tooling. In the tests we ran, the container worked on a Raspberry connected to the battery serial console through a working **RJ45-to-USB serial cable**, but the same approach can be adapted easily to serial device servers such as **Elfin EW/EE-10** or **USR-W610**, because the verified protocol is just a plain serial console with known settings and command flow.

---

## Why this project exists

The motivations behind this project are simple:

* **Expose Pytes console data as clean HTTP JSON** instead of scraping raw serial text in downstream tools.
* **Keep the JSON compact** so Telegraf and InfluxDB only store the metrics that are actually useful.
* **Avoid unnecessary host dependencies**: the service runs in Docker and the serial handling is implemented with Python standard library `termios`, not `pyserial`.
* **Make Telegraf integration easy** by serving a ready-to-ingest `/metrics` payload with a `summary` object and a `batteries` array.

In short, this is a focused bridge between the Pytes console and a time-series stack.

---

## Architecture

```text
Pytes E-BOX console port
        |
        v
RJ45 -> USB serial cable
or serial device server
(Elfin EW/EE-10, USR-W610, similar)
        |
        v
Docker host
        |
        v
pytes-proxy
        |
        v
HTTP JSON (/metrics, /health)
        |
        v
Telegraf -> InfluxDB v2 -> Grafana
```

The implementation currently talks to a **local serial device path** such as `/dev/ttyUSB0` or `/dev/serial/by-id/...`. If you use an Elfin or USR serial-to-Ethernet adapter, the project can be adapted with a small transport change or with a serial-over-TCP bridge on the host. The important part is that the Pytes side has already been validated.

---

## Verified Pytes console protocol

The serial protocol has already been validated in the tested setup:

* `115200` baud
* `8N1`
* no flow control
* command terminator: `\r`
* prompt detected: `PYTES_debug>`
* end marker detected: `Command completed successfully`
* commands used:
  * `pwr`
  * `bat N`

The device echoes the command, so the service reads until the end marker and parses only the useful table rows.

---

## Why the service uses raw serial instead of pyserial

This is a deliberate design choice:

* the communication was already proven with `termios`
* Python standard library is enough
* fewer dependencies means fewer moving parts
* the container stays small and straightforward

---

## How it works

* The poller opens the Pytes console on the configured serial port.
* It sends `pwr` to discover modules and collect per-module data.
* For each detected module, it sends `bat N` to collect the 16 cell voltages.
* The service merges both command outputs into one compact battery object per module.
* It also builds a top-level `summary` section with totals, averages, and cell spread metrics.
* The latest snapshot is kept in memory and returned immediately through HTTP.

The code also includes pragmatic resilience:

* retry logic for incomplete `bat N` reads
* reuse of cached module data when a `pwr` response is partial
* reuse of cached cell data when a module response is incomplete
* exponential backoff after serial/runtime failures

---

## JSON shape

The project intentionally does **not** expose the entire raw Pytes output. The goal is to keep the payload useful and compact.

Current high-level structure:

```json
{
  "host_ts_iso": "2026-03-15T19:44:00+00:00",
  "host_ts_unix_ms": 1742067840000,
  "summary": {
    "total_current_a": -8.228,
    "total_power_w": -436.412,
    "avg_soc_percent": 83.5,
    "min_soc_percent": 82,
    "max_soc_percent": 85,
    "avg_module_voltage_v": 53.0415,
    "min_module_voltage_v": 52.991,
    "max_module_voltage_v": 53.092,
    "avg_module_temperature_c": 28.4,
    "min_cell_voltage_v": 3.312,
    "min_cell_voltage_module_id": 2,
    "min_cell_voltage_cell": 7,
    "max_cell_voltage_v": 3.327,
    "max_cell_voltage_module_id": 1,
    "max_cell_voltage_cell": 11,
    "cell_delta_v": 0.015,
    "avg_cell_voltage_v": 3.3194
  },
  "batteries": [
    {
      "module_id": 1,
      "voltage_v": 53.049,
      "current_a": -2.228,
      "power_w": -118.177,
      "temperature_c": 28.0,
      "soc_percent": 83,
      "capacity_mah": 50000,
      "cell0_v": 3.318,
      "cell1_v": 3.319
    }
  ],
  "last_error": null
}
```

Important idea: downstream tools receive one compact snapshot, not raw CLI dumps.

---

## Endpoints

* `GET /metrics` -> latest full snapshot
* `GET /` -> same as `/metrics`
* `GET /health` -> lightweight health/status payload

Examples:

```bash
curl http://<HOST>:8080/metrics
curl http://<HOST>:8080/health
```

Example `/health` response:

```json
{
  "ok": true,
  "last_error": null,
  "ts": "2026-03-15T19:44:00+00:00"
}
```

---

## Quick start

### Build the image

```bash
docker build -t pytes-service .
```

### Run with Docker Compose

The repository already includes a `docker-compose.yml` with the important runtime choices:

* `restart: unless-stopped`
* `security_opt: [seccomp=unconfined]`
* stable serial device mapping
* bundled Telegraf sidecar

Start it with:

```bash
docker compose up -d
```

### Included Compose service

The current compose file expects this serial path:

```text
/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_A6019ODM-if00-port0
```

This is the path used in the test setup.

---

## Environment variables

Supported by `pytes_service.py`:

* `SERIAL_PORT` -> serial device path, default `/dev/ttyUSB0`
* `SERIAL_BAUD` -> default `115200`
* `SERIAL_TIMEOUT_S` -> per-command read timeout, default `4`
* `POLL_INTERVAL_S` -> polling interval, default `30`
* `MAX_MODULES` -> max module ids to scan, default `16`
* `BAT_RETRIES` -> extra retries for incomplete `bat N` output, default `2`
* `HTTP_HOST` -> HTTP bind address, default `0.0.0.0`
* `HTTP_PORT` -> HTTP port, default `8080`

Influx-related variables used by the bundled Telegraf container:

* `INFLUX_URL`
* `INFLUX_TOKEN`
* `INFLUX_ORG`
* `INFLUX_BUCKET`

See [.env.example](c:/Progetti/Raspberry/pytes-proxy/.env.example) for a minimal example.

---

## Telegraf integration

The repository includes a ready-made Telegraf config at [telegraf/telegraf.conf](c:/Progetti/Raspberry/pytes-proxy/telegraf/telegraf.conf).

The included file currently points to `http://<HOST>:8080/metrics`, so you should adjust that URL for your own host or container networking setup.

The config reads `/metrics` twice:

* once as measurement `summary`
* once as measurement `batteries`

This makes the data model much cleaner in InfluxDB:

* one record for global pack/system summary
* one record per battery module
* `module_id` used as a tag
* cell voltages stored as numeric fields

That is exactly one of the main reasons this project exists: keep ingestion simple and avoid doing parsing gymnastics inside Telegraf.

---

## Adapting it to Elfin EW/EE-10 or USR-W610

Although the current implementation is tested with a direct **RJ45-to-USB serial cable**, the project is a good fit for adapters such as **Elfin EW/EE-10** or **USR-W610**.

Why adaptation is straightforward:

* the Pytes side is already known and stable
* the required serial settings are already verified
* the application logic is independent from the physical wiring
* only the transport layer changes: local serial device vs serial-over-IP

Typical adaptation paths:

* expose the remote serial adapter locally with a bridge such as `socat` or `ser2net`
* replace the low-level transport in `PytesConsole` so commands go to a TCP socket instead of a local tty

So the hard part, understanding the Pytes console protocol and shaping useful JSON, is already done.

---

## Files

* [pytes_service.py](c:/Progetti/Raspberry/pytes-proxy/pytes_service.py) -> main service, polling, parsing, HTTP server
* [docker-compose.yml](c:/Progetti/Raspberry/pytes-proxy/docker-compose.yml) -> service and Telegraf runtime
* [Dockerfile](c:/Progetti/Raspberry/pytes-proxy/Dockerfile) -> container image
* [telegraf/telegraf.conf](c:/Progetti/Raspberry/pytes-proxy/telegraf/telegraf.conf) -> ingestion config

---

## Dependencies

Runtime dependencies are intentionally minimal:

* Python 3
* Docker / Docker Compose
* no third-party Python packages are required by `pytes_service.py`
