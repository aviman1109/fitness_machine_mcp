# fitness-machine-mcp

MCP server that controls BLE FTMS fitness machines and uploads completed workouts to Garmin Connect.

**Supported device classes:**
- Any FTMS treadmill (tested: ADIDAS 1380 — speed control not supported by most firmware; incline + data read works)
- Any FTMS cycling trainer with Power Target Setting (tested: eHuntSun T-SoX — full ERG control, 0–2000 W)
- Garmin HRM-Pro+ — full profile support:
  - Heart Rate + RR intervals (for HRV analysis)
  - RSC (Running Speed & Cadence) — running cadence + stride length
  - CSC (Cycling Speed & Cadence) — cycling cadence fallback
- Any single-sided BLE pedal power meter with Cycling Power Service (0x1818)
  (tested target: Wahoo Powrlink Zero — left pedal, ×2 bilateral estimate)

## Architecture

```
BLE FTMS device ──▶ fitness-machine-mcp ──▶ MCP client (Claude / any AI)
                                             │
                                   Garmin Connect upload (TCX)
```

## Quick Start (Docker)

```bash
docker run -d \
  --network host --privileged \
  -e TREADMILL_BLE_ADDR=XX:XX:XX:XX:XX:XX \
  -e TRAINER_BLE_ADDR=XX:XX:XX:XX:XX:XX \
  -e HRM_BLE_ADDR=XX:XX:XX:XX:XX:XX \
  -e GARMIN_TOKEN_PATH=/tokens/garmin \
  -v /path/to/tokens:/tokens \
  -p 38097:38097 \
  fitness-machine-mcp
```

`--network host --privileged` is required for BLE access on Linux.

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `TREADMILL_BLE_ADDR` | — | BLE MAC address of your FTMS treadmill |
| `TRAINER_BLE_ADDR` | — | BLE MAC address of your FTMS cycling trainer |
| `HRM_BLE_ADDR` | — | BLE MAC address of your Garmin HRM-Pro+ |
| `POWER_METER_BLE_ADDR` | *(empty)* | BLE MAC of pedal power meter — leave unset to skip |
| `POWER_METER_SIDE` | `left` | Which pedal the meter is on (`left` or `right`); power is doubled for bilateral estimate |
| `GARMIN_TOKEN_PATH` | `~/.garmin-tokens` | Path to Garmin Connect auth token directory |
| `TREADMILL_WORKOUTS_DIR` | `/app/workouts` | Directory containing workout JSON files |
| `PORT` | `38097` | HTTP port |
| `MCP_HOST` | `0.0.0.0` | Bind address |
| `MCP_PATH` | `/mcp` | MCP endpoint path |
| `MCP_ALLOWED_HOSTS` | `127.0.0.1:*,localhost:*` | Allowed Host headers (comma-separated) |
| `MCP_ALLOWED_ORIGINS` | `http://127.0.0.1:*,http://localhost:*` | Allowed CORS origins |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | *(disabled)* | OpenTelemetry collector endpoint |
| `OTEL_SERVICE_NAME` | `fitness-machine-mcp` | Service name for telemetry |
| `OTEL_RESOURCE_ATTRIBUTES` | `service.namespace=ftms-mcp` | Additional OTEL resource attributes |

## MCP Tools

| Tool | Sport | Description |
|---|---|---|
| `list_workouts` | both | List available workout plans |
| `get_workout(workout_id)` | both | Full workout plan details |
| `create_workout(...)` | both | Create or update a workout plan |
| `delete_workout(workout_id)` | both | Delete a workout plan |
| `start_workout(workout_id)` | both | Connect to device and start the workout |
| `get_status` | both | Current state, metrics, HR, distance |
| `stop_workout` | both | Emergency stop |
| `set_speed(speed_kmh)` | running | Direct speed override (device must be running) |
| `set_incline(incline_pct)` | running | Direct incline override |
| `set_target_power(power_w)` | cycling | Direct ERG power override |

## Workout File Format

### Running

```json
{
  "name": "Z2 Run 45min",
  "sport": "running",
  "incline_pct": 1.0,
  "steps": [
    {"name": "Warm-up",   "duration_secs": 300,  "speed_kmh": 8.0},
    {"name": "Z2 Run",    "duration_secs": 2400, "speed_kmh": 10.0},
    {"name": "Cool-down", "duration_secs": 300,  "speed_kmh": 7.0}
  ]
}
```

### Cycling (ERG)

```json
{
  "name": "Sweet Spot 60min",
  "sport": "cycling",
  "steps": [
    {"name": "Warm-up",   "duration_secs": 600,  "power_w": 140},
    {"name": "SS Block",  "duration_secs": 1800, "power_w": 210},
    {"name": "Cool-down", "duration_secs": 600,  "power_w": 120}
  ]
}
```

`power_w` values are in watts. Typical targets: 55–75 % FTP = endurance, 88–95 % FTP = sweet spot.

## Cycling Workout Start Behaviour

When `start_workout` is called for a cycling plan:

1. Trainer connects + `REQUEST_CONTROL` + `START_RESUME`
2. Status becomes `ready` — HRM-Pro+ and power meter are already streaming
3. Server waits up to 120 s for cadence > 0 (pedal detected from power meter, HRM-Pro+ CSC, or trainer internal — in that priority order)
4. Once pedaling is detected, the workout timer starts and the first step's ERG power is set

The warm-up is the **first step** in the workout JSON. There is no engine-injected warm-up.

## Sensor Data Priority

| Metric | Primary source | Fallback |
|---|---|---|
| Power | Pedal power meter (`power_w_pm`) | Trainer FTMS (`power_w`) |
| Cadence | Pedal power meter (`cadence_rpm_pm`) | HRM-Pro+ CSC (`cadence_rpm_csc`) → Trainer (`cadence_rpm`) |
| Heart Rate | HRM-Pro+ (`hr_bpm`) | Trainer FTMS |
| HRV (RR) | HRM-Pro+ HR char RR flag (`rr_latest_ms`) | — |
| Running cadence | HRM-Pro+ RSC (`cadence_spm`) | — |

All fields are always present in `get_status`. Zero values mean the source is not connected or has not yet reported.

TCX export uses the highest-priority non-zero value for power and cadence.

## Pairing the Wahoo Powrlink Zero

1. Fit the pedals and spin them once to wake the meter
2. On the host machine:
   ```bash
   python3 -c "
   import asyncio
   from bleak import BleakScanner
   async def scan():
       devices = await BleakScanner.discover(timeout=10)
       for d in devices:
           print(d.address, d.name)
   asyncio.run(scan())
   "
   ```
3. Find the device named `WAHOO POWRLINK` (or similar) and copy its MAC address
4. Add to 1Password vault `Casper MCP` → item `fitness-machine` → field `power_meter_ble_addr`
5. Regenerate secrets: `bash ~/ai-platform/mcps/refresh-secrets.sh fitness-machine-mcp`
6. Rebuild container: `cd ~/ai-platform/mcps && docker compose up -d --build fitness-machine-mcp`

## Notes on FTMS Speed Control

Most consumer treadmills advertise FTMS Speed Target Setting but silently ignore the command — the firmware only uses it to report data and accept incline changes. This is a [known industry-wide issue](https://github.com/cagnulein/qdomyos-zwift/issues/2615). This server handles it gracefully (logs a warning, does not crash).

Cycling trainer ERG (`Set Target Power`, opcode `0x05`) works correctly on devices that properly implement FTMS target features.

## Garmin Upload

Completed workouts are exported as TCX and uploaded automatically:

- Running → `Sport="Running"`, speed + cadence in TPX extension
- Cycling → `Sport="Biking"`, power in `<Watts>` TPX extension field

Pre-authenticate with [garth](https://github.com/matin/garth) and point `GARMIN_TOKEN_PATH` at the resulting token directory.

## Monitoring

OpenTelemetry tracing is optional. Set `OTEL_EXPORTER_OTLP_ENDPOINT` to enable trace export. If unset, the server starts normally without telemetry.

## License

MIT
