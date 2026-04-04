"""Workout execution engine."""
from __future__ import annotations
import asyncio, json, logging, os, time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from fitness_machine_mcp.ble import BLEManager, TrainerBLEManager, LiveData

log = logging.getLogger("fitness_machine.engine")

WORKOUTS_DIR = Path(os.environ.get(
    "TREADMILL_WORKOUTS_DIR",
    str(Path(__file__).parent.parent.parent / "workouts")
))

# How long to wait for the rider to start pedaling before aborting (seconds)
_PEDAL_WAIT_TIMEOUT_SECS = 120


@dataclass
class DataPoint:
    ts:            datetime
    elapsed_sec:   int
    speed_kmh:     float
    incline_pct:   float
    hr_bpm:        int
    cadence_spm:   int
    stride_len_m:  float
    distance_m:    float
    power_w:       int   = 0   # trainer ERG reported power
    cadence_rpm:   int   = 0   # trainer internal cadence
    # Extended sensor data
    rr_ms:         int   = 0   # latest RR interval (ms) — for HRV analysis
    power_w_pm:    int   = 0   # pedal power meter (preferred; 0 if not connected)
    cadence_rpm_pm: int  = 0   # pedal power meter cadence (preferred; 0 if not connected)


@dataclass
class WorkoutState:
    status:         str   = "idle"   # idle|connecting|ready|running|done|error
    workout_name:   str   = ""
    sport:          str   = "running"
    step_name:      str   = ""
    step_index:     int   = 0
    step_total:     int   = 0
    elapsed_sec:    int   = 0
    remaining_sec:  int   = 0
    speed_kmh:      float = 0.0
    incline_pct:    float = 0.0
    hr_bpm:         int   = 0
    cadence_spm:    int   = 0
    # Cycling
    cadence_rpm:    int   = 0        # trainer internal cadence
    cadence_rpm_pm: int   = 0        # pedal power meter cadence (preferred)
    cadence_rpm_csc: int  = 0        # HRM-Pro+ CSC cadence (fallback)
    power_w:        int   = 0        # trainer ERG reported power
    power_w_pm:     int   = 0        # pedal power meter power (preferred)
    target_power_w: int   = 0        # current step ERG target
    distance_m:     float = 0.0
    # HRV
    rr_latest_ms:   int   = 0        # latest RR interval (ms) — available for both sports
    # Result
    garmin_activity_id: Optional[int] = None
    error:          str   = ""


class WorkoutEngine:
    def __init__(self):
        self._state = WorkoutState()
        self._ble: Optional[BLEManager] = None
        self._trainer_ble: Optional[TrainerBLEManager] = None
        self._data: list[DataPoint] = []
        self._start_time: Optional[datetime] = None
        self._session_distance_m: float = 0.0
        self._latest: Optional[LiveData] = None
        self._task: Optional[asyncio.Task] = None

    @property
    def state(self) -> WorkoutState:
        return self._state

    def list_workouts(self) -> list[dict]:
        plans = []
        for f in sorted(WORKOUTS_DIR.glob("*.json")):
            try:
                with f.open() as fh:
                    p = json.load(fh)
                total = sum(s["duration_secs"] for s in p["steps"])
                plans.append({"id": f.stem, "name": p["name"],
                               "description": p.get("description", ""),
                               "duration_secs": total})
            except Exception as e:
                log.warning("Could not load workout %s: %s", f, e)
        return plans

    def get_workout(self, workout_id: str) -> dict | None:
        path = WORKOUTS_DIR / f"{workout_id}.json"
        if not path.exists():
            return None
        with path.open() as fh:
            return json.load(fh)

    def save_workout(self, workout_id: str, plan: dict) -> None:
        WORKOUTS_DIR.mkdir(parents=True, exist_ok=True)
        path = WORKOUTS_DIR / f"{workout_id}.json"
        with path.open("w") as fh:
            json.dump(plan, fh, ensure_ascii=False, indent=2)
        log.info("Saved workout: %s", path)

    def delete_workout(self, workout_id: str) -> bool:
        path = WORKOUTS_DIR / f"{workout_id}.json"
        if not path.exists():
            return False
        path.unlink()
        log.info("Deleted workout: %s", path)
        return True

    def _on_ble_data(self, live: LiveData):
        self._latest = live
        self._state.speed_kmh      = live.speed_kmh
        self._state.hr_bpm         = live.hr_bpm
        self._state.cadence_spm    = live.cadence_spm
        self._state.incline_pct    = live.incline_pct
        self._state.power_w        = live.power_w
        self._state.cadence_rpm    = live.cadence_rpm
        self._state.power_w_pm     = live.power_w_pm
        self._state.cadence_rpm_pm = live.cadence_rpm_pm
        self._state.cadence_rpm_csc = live.cadence_rpm_csc
        if live.rr_intervals_ms:
            self._state.rr_latest_ms = live.rr_intervals_ms[-1]

    def _record(self, elapsed: int):
        if not self._latest:
            return
        self._data.append(DataPoint(
            ts             = datetime.now(timezone.utc),
            elapsed_sec    = elapsed,
            speed_kmh      = self._latest.speed_kmh,
            incline_pct    = self._latest.incline_pct,
            hr_bpm         = self._latest.hr_bpm,
            cadence_spm    = self._latest.cadence_spm,
            stride_len_m   = self._latest.stride_len_m,
            distance_m     = self._session_distance_m,
            power_w        = self._latest.power_w,
            cadence_rpm    = self._latest.cadence_rpm,
            rr_ms          = self._latest.rr_intervals_ms[-1] if self._latest.rr_intervals_ms else 0,
            power_w_pm     = self._latest.power_w_pm,
            cadence_rpm_pm = self._latest.cadence_rpm_pm,
        ))

    def start(self, workout_id: str) -> bool:
        if self._state.status not in ("idle", "done", "error"):
            return False
        self._task = asyncio.create_task(self._run(workout_id))
        return True

    async def emergency_stop(self):
        # 1. Cancel workout task and wait for it to finish
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None

        # 2. Send stop command to device (best-effort)
        if self._ble:
            try:
                await self._ble.stop()
            except Exception:
                pass

        if self._trainer_ble:
            try:
                await self._trainer_ble.stop()
            except Exception:
                pass

        # 3. Disconnect all BLE (releases connections and PM reconnect loop)
        if self._ble:
            try:
                await self._ble.disconnect()
            except Exception:
                pass
            self._ble = None

        if self._trainer_ble:
            try:
                await self._trainer_ble.disconnect()
            except Exception:
                pass
            self._trainer_ble = None

        self._state.status = "idle"

    async def set_target_power(self, watts: int) -> bool:
        if not self._trainer_ble:
            raise RuntimeError("Trainer not connected")
        return await self._trainer_ble.set_target_power(watts)

    async def _run(self, workout_id: str):
        plan_path = WORKOUTS_DIR / f"{workout_id}.json"
        if not plan_path.exists():
            self._state.status = "error"
            self._state.error = f"Workout not found: {workout_id}"
            return

        with plan_path.open() as fh:
            plan = json.load(fh)

        sport = plan.get("sport", "running").lower()
        steps = plan["steps"]
        self._state.workout_name = plan["name"]
        self._state.sport        = sport
        self._state.step_total   = len(steps)
        self._state.status       = "connecting"
        self._state.error        = ""
        self._data = []
        self._session_distance_m = 0.0

        if sport == "cycling":
            await self._run_cycling(plan)
        else:
            await self._run_treadmill(plan)

    async def _run_cycling(self, plan: dict):
        """Execute a cycling ERG workout on the T-SoX trainer.

        Flow:
          1. Connect trainer + HRM-Pro+ (+ pedal power meter if configured)
          2. REQUEST_CONTROL → START_RESUME → status=ready
          3. Wait for cadence > 0 (rider starts pedaling) — workout timer begins here
          4. Execute steps: set target power, record data each second
          5. On complete: stop trainer, upload TCX to Garmin
        """
        steps = plan["steps"]

        try:
            self._trainer_ble = TrainerBLEManager(on_data=self._on_ble_data)
            await self._trainer_ble.connect()
            self._state.status = "ready"

            await self._trainer_ble.request_control()
            await self._trainer_ble.start()

            # ── Wait for rider to start pedaling ─────────────────────────────
            self._state.step_name = "等待踩踏…"
            log.info("Waiting for pedaling (cadence > 0, timeout=%ds)…",
                     _PEDAL_WAIT_TIMEOUT_SECS)
            for _ in range(_PEDAL_WAIT_TIMEOUT_SECS):
                await asyncio.sleep(1.0)
                if self._latest:
                    cadence = (self._latest.cadence_rpm_pm      # pedal meter (best)
                               or self._latest.cadence_rpm_csc  # HRM-Pro+ CSC
                               or self._latest.cadence_rpm)     # trainer internal
                    if cadence and cadence > 0:
                        log.info("Pedaling detected (%d rpm) — starting workout timer", cadence)
                        break
            else:
                raise RuntimeError(
                    f"Timeout: no pedaling detected within {_PEDAL_WAIT_TIMEOUT_SECS}s"
                )

            # ── Execute steps ─────────────────────────────────────────────────
            self._state.status = "running"
            self._start_time = datetime.now(timezone.utc)
            total_elapsed = 0

            for i, step in enumerate(steps):
                self._state.step_index    = i
                self._state.step_name     = step["name"]
                target_power = step["power_w"]
                duration     = step["duration_secs"]
                self._state.target_power_w = target_power

                log.info("Step %d/%d: %s — %dW for %ds",
                         i + 1, len(steps), step["name"], target_power, duration)
                await self._trainer_ble.set_target_power(target_power)

                step_start = time.monotonic()
                while (elapsed := time.monotonic() - step_start) < duration:
                    self._state.elapsed_sec   = total_elapsed + int(elapsed)
                    self._state.remaining_sec = int(duration - elapsed)

                    if self._latest:
                        if self._latest.distance_m == 0:
                            self._session_distance_m += self._latest.speed_kmh / 3.6
                            self._state.distance_m = self._session_distance_m
                        else:
                            self._state.distance_m = self._latest.distance_m

                    self._record(total_elapsed + int(elapsed))
                    await asyncio.sleep(1.0)

                total_elapsed += duration

            log.info("Cycling workout complete. Stopping trainer.")
            await self._trainer_ble.stop()
            self._state.status = "done"

        except asyncio.CancelledError:
            log.info("Cycling workout cancelled")
            if self._trainer_ble:
                try:
                    await self._trainer_ble.stop()
                except Exception:
                    pass
            self._state.status = "idle"
            return
        except Exception as e:
            log.exception("Cycling workout error: %s", e)
            self._state.status = "error"
            self._state.error  = str(e)
            if self._trainer_ble:
                try:
                    await self._trainer_ble.stop()
                except Exception:
                    pass
        finally:
            if self._trainer_ble:
                await self._trainer_ble.disconnect()

        if self._data and self._state.status == "done":
            await self._upload(plan)

    async def _run_treadmill(self, plan: dict):
        """Record a treadmill running workout (monitor-only, no machine control).

        Flow:
          1. Connect treadmill + HRM-Pro+
          2. Wait for belt speed > 0 (user starts the machine manually)
          3. Execute steps: record data each second, advance step timer
             — no speed/incline commands are sent to the machine
          4. On complete: upload TCX to Garmin
        """
        steps = plan["steps"]

        try:
            self._ble = BLEManager(on_data=self._on_ble_data)
            await self._ble.connect()
            self._state.status = "ready"

            # ── Wait for user to start the belt ──────────────────────────────
            self._state.step_name = "等待跑步機啟動…"
            log.info("Waiting for treadmill belt (speed > 0, timeout=120s)…")
            for _ in range(120):
                await asyncio.sleep(1.0)
                if self._ble.live.speed_kmh > 0:
                    log.info("Belt moving (%.1f km/h) — starting recording",
                             self._ble.live.speed_kmh)
                    break
            else:
                raise RuntimeError("Timeout: belt did not start within 120s")

            # ── Record steps ─────────────────────────────────────────────────
            self._state.status = "running"
            self._start_time = datetime.now(timezone.utc)
            total_elapsed = 0

            for i, step in enumerate(steps):
                self._state.step_index = i
                self._state.step_name  = step["name"]
                duration = step["duration_secs"]

                log.info("Step %d/%d: %s — recording for %ds",
                         i + 1, len(steps), step["name"], duration)

                step_start = time.monotonic()
                while (elapsed := time.monotonic() - step_start) < duration:
                    self._state.elapsed_sec   = total_elapsed + int(elapsed)
                    self._state.remaining_sec = int(duration - elapsed)

                    if self._latest:
                        self._session_distance_m += self._latest.speed_kmh / 3.6
                    self._state.distance_m = self._session_distance_m

                    self._record(total_elapsed + int(elapsed))
                    await asyncio.sleep(1.0)

                total_elapsed += duration

            log.info("Treadmill recording complete.")
            self._state.status = "done"

        except asyncio.CancelledError:
            log.info("Treadmill workout cancelled")
            if self._ble:
                try:
                    await self._ble.stop()
                except Exception:
                    pass
            self._state.status = "idle"
            return
        except Exception as e:
            log.exception("Treadmill workout error: %s", e)
            self._state.status = "error"
            self._state.error  = str(e)
            if self._ble:
                try:
                    await self._ble.stop()
                except Exception:
                    pass
        finally:
            if self._ble:
                await self._ble.disconnect()

        if self._data and self._state.status == "done":
            await self._upload(plan)

    async def _upload(self, plan: dict):
        try:
            from fitness_machine_mcp.tcx import build_tcx
            from fitness_machine_mcp.upload import upload_activity, set_activity_type

            sport = plan.get("sport", "running").lower()
            tcx_path = Path(f"/tmp/fitness_{sport}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.tcx")
            tcx_bytes = build_tcx(plan, self._data, self._start_time)
            tcx_path.write_bytes(tcx_bytes)
            log.info("TCX written: %s (%d bytes)", tcx_path, len(tcx_bytes))

            activity_id = await asyncio.get_event_loop().run_in_executor(
                None, upload_activity, str(tcx_path)
            )
            self._state.garmin_activity_id = activity_id
            log.info("Uploaded to Garmin. Activity ID: %s", activity_id)

            if activity_id:
                # Map sport to Garmin activity type key
                type_key = "treadmill_running" if sport == "running" else "indoor_cycling"
                await asyncio.get_event_loop().run_in_executor(
                    None, set_activity_type, activity_id, type_key
                )
        except Exception as e:
            log.error("Upload failed: %s", e)
            self._state.error = f"Upload failed: {e}"
