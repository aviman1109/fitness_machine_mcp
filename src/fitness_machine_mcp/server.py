"""Treadmill MCP server — BLE control + workout tracking."""
from __future__ import annotations
import asyncio
import logging
import os
import sys
from typing import Optional

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel
import uvicorn

from fitness_machine_mcp.engine import WorkoutEngine
from fitness_machine_mcp.otel import setup_otel
from fitness_machine_mcp.devices import registry, scan_devices as _scan_devices, VALID_ROLES

_LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, _LOG_LEVEL, logging.INFO),
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("fitness_machine.server")

# ── Globals ───────────────────────────────────────────────────────────────────

engine = WorkoutEngine()

# ── FastMCP setup ─────────────────────────────────────────────────────────────

SERVER_INSTRUCTIONS = """
This MCP server controls BLE FTMS fitness machines (treadmill + cycling trainer) and uploads completed workouts to Garmin Connect.

## Running (treadmill)
The treadmill does NOT support speed control from software — the user sets speed manually on the machine.
create_workout (sport=running) stores a session plan: step names, durations, and reference speed/incline for coaching.
start_workout connects and records data for the planned duration. Steps are for timing and coaching reference only.
Use get_status to monitor HR/speed/distance in real time.

## Cycling (ERG trainer)
create_workout (sport=cycling, steps with power_w) defines ERG power targets executed automatically.
start_workout runs the ERG plan step by step. set_target_power for live overrides.

## Workout management
create_workout and delete_workout work at any time (except deleting the currently running workout).
list_workouts and get_workout are always available.

## Both sports
After completion the activity is auto-uploaded to Garmin Connect.
""".strip()

app = FastMCP(name="Fitness Machine MCP", instructions=SERVER_INSTRUCTIONS)

_allowed_hosts  = os.environ.get("MCP_ALLOWED_HOSTS",  "127.0.0.1:*,localhost:*")
_allowed_origins = os.environ.get("MCP_ALLOWED_ORIGINS", "http://127.0.0.1:*,http://localhost:*")
app.settings.transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=[h.strip() for h in _allowed_hosts.split(",")],
    allowed_origins=[o.strip() for o in _allowed_origins.split(",")],
)
app.settings.stateless_http = True
app.settings.json_response  = True
app.settings.streamable_http_path = os.environ.get("MCP_PATH", "/mcp")

# ── Schemas ───────────────────────────────────────────────────────────────────

class WorkoutStep(BaseModel):
    name: str
    duration_secs: int
    # Running: coaching reference only — NOT sent to the machine (user adjusts manually)
    speed_kmh: Optional[float] = None
    # Cycling ERG: actual target power sent to the trainer via BLE FTMS
    power_w: Optional[int] = None


# ── MCP Tools ─────────────────────────────────────────────────────────────────

@app.tool(
    description="List available treadmill workout plans.",
    annotations={"readOnlyHint": True, "idempotentHint": True},
)
def list_workouts() -> list[dict]:
    return engine.list_workouts()


@app.tool(
    description="Get full details of a workout plan by ID.",
    annotations={"readOnlyHint": True, "idempotentHint": True},
)
def get_workout(workout_id: str) -> dict:
    plan = engine.get_workout(workout_id)
    if plan is None:
        return {"error": f"Workout not found: {workout_id}"}
    return plan


@app.tool(
    description=(
        "Create or update a workout plan. Supports both running and cycling.\n"
        "\n"
        "sport='cycling': power_w per step is the ERG target — sent to the trainer automatically.\n"
        "\n"
        "sport='running': TREADMILL HARDWARE LIMITATION — speed and incline CANNOT be controlled "
        "by software. The user must adjust speed/incline manually on the machine. "
        "speed_kmh and incline_pct in steps are coaching reference values only (shown to coach, "
        "never sent to the machine). Steps are used for timing and naming only.\n"
        "\n"
        "Cycling example: [{\"name\":\"暖身\",\"duration_secs\":1200,\"power_w\":140}]\n"
        "Running example: [{\"name\":\"Z2 10min\",\"duration_secs\":600,\"speed_kmh\":10.0}]"
    ),
)
def create_workout(
    workout_id: str,
    name: str,
    steps: list[WorkoutStep],
    description: str = "",
    sport: str = "cycling",
    incline_pct: float = 0.0,
) -> dict:
    """Create or overwrite a workout plan. workout_id becomes the filename (no spaces)."""
    if not workout_id or not workout_id.replace("_", "").replace("-", "").isalnum():
        return {"ok": False, "error": "workout_id must be alphanumeric with _ or -"}
    if not steps:
        return {"ok": False, "error": "steps cannot be empty"}
    sport_lower = sport.lower()
    if sport_lower not in ("cycling", "running"):
        return {"ok": False, "error": "sport must be 'cycling' or 'running'"}
    for s in steps:
        if s.duration_secs <= 0 or s.duration_secs > 14400:
            return {"ok": False, "error": f"Step '{s.name}': duration_secs must be 1–14400"}
        if sport_lower == "cycling":
            if s.power_w is None:
                return {"ok": False, "error": f"Cycling step '{s.name}' missing power_w"}
            if not (0 <= s.power_w <= 2000):
                return {"ok": False, "error": f"Cycling step '{s.name}': power_w must be 0–2000 W"}
    plan: dict = {
        "name": name,
        "description": description,
        "sport": sport_lower,
        "steps": [s.model_dump(exclude_none=True) for s in steps],
    }
    if sport_lower == "running" and incline_pct != 0.0:
        plan["incline_pct"] = incline_pct
    engine.save_workout(workout_id, plan)
    total = sum(s.duration_secs for s in steps)
    return {"ok": True, "workout_id": workout_id, "sport": sport_lower,
            "steps": len(steps), "total_secs": total}


@app.tool(description="Delete a workout plan by ID. Cannot delete the currently running workout.")
def delete_workout(workout_id: str) -> dict:
    plan = engine.get_workout(workout_id)
    if plan is None:
        return {"ok": False, "error": f"Workout not found: {workout_id}"}
    if (engine.state.status in ("running", "connecting", "ready")
            and engine.state.workout_name == plan.get("name")):
        return {"ok": False, "error": f"Cannot delete '{workout_id}' — it is currently running"}
    deleted = engine.delete_workout(workout_id)
    return {"ok": deleted, "deleted": workout_id if deleted else None,
            "error": None if deleted else f"Workout not found: {workout_id}"}


@app.tool(description=(
    "Start a workout by ID.\n"
    "Running: insert safety key and start the belt manually — this MCP only records data, "
    "it does NOT control treadmill speed or incline.\n"
    "Cycling: start pedaling — ERG power targets from the plan are sent to the trainer automatically."
))
async def start_workout(workout_id: str) -> dict:
    """Start a workout. Returns status dict."""
    state = engine.state
    if state.status in ("running", "connecting", "ready"):
        return {"ok": False, "error": f"Workout already active: {state.workout_name}"}
    plan = engine.get_workout(workout_id)
    if plan is None:
        return {"ok": False, "error": f"Workout not found: {workout_id}"}
    ok = engine.start(workout_id)
    if not ok:
        return {"ok": False, "error": "Could not start workout (check status)"}
    sport = plan.get("sport", "running")
    if sport == "cycling":
        return {"ok": True, "started": workout_id, "message": "訓練台 ERG 課表開始，開始踩踏"}
    return {"ok": True, "started": workout_id, "message": "插好安全鑰匙，課表開始執行"}


@app.tool(
    description="Get current treadmill workout status.",
    annotations={"readOnlyHint": True, "idempotentHint": True},
)
def get_status() -> dict:
    s = engine.state
    base = {
        "status":        s.status,
        "sport":         s.sport,
        "workout":       s.workout_name,
        "step":          f"{s.step_name} ({s.step_index+1}/{s.step_total})" if s.step_total else "",
        "elapsed_sec":   s.elapsed_sec,
        "remaining_sec": s.remaining_sec,
        "hr_bpm":        s.hr_bpm,
        "rr_latest_ms":  s.rr_latest_ms,   # latest RR interval (ms); 0 if HRM not connected
        "distance_m":    round(s.distance_m, 1),
        "garmin_activity_id": s.garmin_activity_id,
        "error":         s.error,
    }
    if s.sport == "cycling":
        base["power_w"]           = s.power_w
        base["power_w_pm"]        = s.power_w_pm
        base["target_power_w"]    = s.target_power_w
        base["cadence_rpm"]       = s.cadence_rpm
        base["cadence_rpm_pm"]    = s.cadence_rpm_pm
        base["cadence_rpm_csc"]   = s.cadence_rpm_csc
        base["speed_kmh"]         = round(s.speed_kmh, 1)
        base["pedal_balance_pct"] = s.pedal_balance_pct   # left % (0.0 if not reported)
        base["ant_active"]        = s.ant_active
        if s.torque_eff_l or s.torque_eff_r:
            base["torque_eff_l"]    = s.torque_eff_l
            base["torque_eff_r"]    = s.torque_eff_r
        if s.pedal_smooth_l or s.pedal_smooth_r:
            base["pedal_smooth_l"]  = s.pedal_smooth_l
            base["pedal_smooth_r"]  = s.pedal_smooth_r
    else:
        base["speed_kmh"]       = round(s.speed_kmh, 1)
        base["incline_pct"]     = round(s.incline_pct, 1)
        base["cadence_spm"]     = s.cadence_spm
        base["stride_len_m"]    = round(s.stride_len_m, 3) if s.stride_len_m else 0.0
        base["ant_active"]      = s.ant_active
    return base


@app.tool(description="Stop the active workout, upload collected data to Garmin Connect, and disconnect all BLE devices. Safe to call at any time — uploads whatever data was recorded so far.")
async def stop_workout() -> dict:
    await engine.emergency_stop()
    garmin_id = engine.state.garmin_activity_id
    err = engine.state.error or None
    return {
        "stopped": True,
        "garmin_activity_id": garmin_id,
        "uploaded": garmin_id is not None,
        "error": err,
    }


@app.tool(description="Directly set trainer ERG target power (watts). Trainer must be running.")
async def set_target_power(power_w: int) -> dict:
    """Set trainer ERG power directly. Range: 0–2000W."""
    if power_w < 0 or power_w > 2000:
        return {"ok": False, "error": "Power must be 0–2000W"}
    try:
        ok = await engine.set_target_power(power_w)
        return {"ok": ok, "power_w": power_w}
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}


# ── Device management tools ───────────────────────────────────────────────────

@app.tool(
    description=(
        "Scan for nearby BLE fitness devices and identify their capabilities. "
        "Returns devices with suggested roles (trainer/treadmill/hrm/power_meter). "
        "Use pair_device() afterwards to assign a device to a role."
    ),
    annotations={"readOnlyHint": True},
)
async def scan_devices(timeout_secs: int = 12) -> list[dict]:
    """Scan BLE and return identifiable fitness devices sorted by signal strength."""
    return await _scan_devices(timeout=float(timeout_secs))


@app.tool(
    description=(
        "Pair a BLE device to a role and save it persistently. "
        "Roles: trainer, treadmill, hrm, power_meter. "
        "addr is the BLE MAC address from scan_devices(). "
        "name is optional display label."
    ),
)
def pair_device(role: str, addr: str, name: str = "") -> dict:
    """Assign a BLE device address to a role. Persists across restarts."""
    if role not in VALID_ROLES:
        return {"ok": False, "error": f"Unknown role '{role}'. Valid: {sorted(VALID_ROLES)}"}
    if not addr:
        return {"ok": False, "error": "addr is required"}
    try:
        registry.assign(role, addr.upper(), name)
        return {"ok": True, "role": role, "addr": addr.upper(), "name": name or addr}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.tool(description="Remove a device pairing by role (trainer/treadmill/hrm/power_meter).")
def unpair_device(role: str) -> dict:
    """Remove a device assignment. The device will not be connected in future workouts."""
    ok = registry.forget(role)
    return {"ok": ok, "role": role,
            "error": None if ok else f"Role '{role}' was not paired"}


@app.tool(
    description="List all paired devices with their roles and addresses.",
    annotations={"readOnlyHint": True, "idempotentHint": True},
)
def list_devices() -> dict:
    """Return all paired device assignments."""
    return registry.all_paired()


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    setup_otel()

    host      = os.environ.get("MCP_HOST", "0.0.0.0")
    port      = int(os.environ.get("PORT", "38097"))
    transport = os.environ.get("MCP_TRANSPORT", "http").lower()

    log.info("Fitness Machine MCP starting — transport=%s port=%d", transport, port)

    if transport == "stdio":
        app.run()
        return

    if transport in {"http", "streamable-http"}:
        http_app = app.streamable_http_app()
        uvicorn.run(http_app, host=host, port=port, log_config=None)
        return

    raise ValueError(f"Unsupported transport '{transport}'")


if __name__ == "__main__":
    main()
