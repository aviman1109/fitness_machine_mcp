"""Workout execution engine."""
from __future__ import annotations
import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from fitness_machine_mcp.ble import BLEManager, TrainerBLEManager, LiveData
from fitness_machine_mcp.ant import ANTManager

log = logging.getLogger("fitness_machine.engine")

# ANT+ readings older than this are considered stale and ignored.
_ANT_STALE_SECS = 3.0

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
    power_w_pm:         int   = 0    # pedal power meter (preferred; 0 if not connected)
    cadence_rpm_pm:     int   = 0    # pedal power meter cadence (preferred; 0 if not connected)
    pedal_balance_pct:  float = 0.0  # left-side power % (0.0 if not reported)
    torque_eff_l:       float = 0.0  # ANT+ torque effectiveness left (%)
    torque_eff_r:       float = 0.0  # ANT+ torque effectiveness right (%)
    pedal_smooth_l:     float = 0.0  # ANT+ pedal smoothness left (%)
    pedal_smooth_r:     float = 0.0  # ANT+ pedal smoothness right (%)


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
    stride_len_m:   float = 0.0
    # Cycling
    cadence_rpm:    int   = 0        # trainer internal cadence
    cadence_rpm_pm: int   = 0        # pedal power meter cadence (preferred)
    cadence_rpm_csc: int  = 0        # HRM-Pro+ CSC cadence (fallback)
    power_w:        int   = 0        # trainer ERG reported power
    power_w_pm:          int   = 0        # pedal power meter power (preferred)
    target_power_w:      int   = 0        # current step ERG target
    distance_m:          float = 0.0
    pedal_balance_pct:   float = 0.0      # left-side power % (0.0 if not reported)
    # ANT+ extended (populated when ANT+ dongle active)
    ant_active:          bool  = False
    torque_eff_l:        float = 0.0
    torque_eff_r:        float = 0.0
    pedal_smooth_l:      float = 0.0
    pedal_smooth_r:      float = 0.0
    # HRV
    rr_latest_ms:        int   = 0        # latest RR interval (ms) — available for both sports
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
        self._current_plan: Optional[dict] = None
        self._latest_ts: float = 0.0  # monotonic timestamp of last BLE data packet
        self._ant: Optional[ANTManager] = None

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
        self._latest_ts = time.monotonic()
        self._state.speed_kmh      = live.speed_kmh
        self._state.hr_bpm         = live.hr_bpm
        self._state.cadence_spm    = live.cadence_spm
        self._state.incline_pct    = live.incline_pct
        self._state.power_w        = live.power_w
        self._state.cadence_rpm    = live.cadence_rpm
        self._state.power_w_pm     = live.power_w_pm
        self._state.cadence_rpm_pm    = live.cadence_rpm_pm
        self._state.cadence_rpm_csc   = live.cadence_rpm_csc
        self._state.pedal_balance_pct = live.pedal_balance_pct
        if live.rr_intervals_ms:
            self._state.rr_latest_ms = live.rr_intervals_ms[-1]
        if live.stride_len_m > 0:
            self._state.stride_len_m = live.stride_len_m
        if live.cadence_spm > 0:
            self._state.cadence_spm = live.cadence_spm

        if self._ant:
            ant = self._ant.data
            now = time.monotonic()
            hr_fresh  = ant.last_hr_ts  > 0 and (now - ant.last_hr_ts)  < _ANT_STALE_SECS
            pwr_fresh = ant.last_pwr_ts > 0 and (now - ant.last_pwr_ts) < _ANT_STALE_SECS

            # HR: ANT+ primary — HRM-Pro+'s native protocol, prefer over BLE stack latency.
            # Only apply when the reading is fresh to avoid stale overrides after signal loss.
            if hr_fresh and ant.hr_bpm > 0:
                self._state.hr_bpm = ant.hr_bpm
            # RR: ANT+ primary (page 0x04 derived)
            if hr_fresh and ant.rr_ms > 0:
                self._state.rr_latest_ms = ant.rr_ms
            # Running cadence/stride: ANT+ RSC primary
            if ant.cadence_spm > 0:
                self._state.cadence_spm = ant.cadence_spm
            if ant.stride_len_m > 0:
                self._state.stride_len_m = ant.stride_len_m
            # Cycling power/cadence: BLE pedal meter is primary (bilateral + balance).
            # When BLE PM drops (power_w_pm == 0), fall back to ANT+ from the same
            # device before resorting to the trainer's reported value.
            if self._state.power_w_pm == 0 and pwr_fresh and ant.power_w > 0:
                self._state.power_w_pm = ant.power_w
                if self._state.cadence_rpm_pm == 0 and ant.cadence_rpm > 0:
                    self._state.cadence_rpm_pm = ant.cadence_rpm
            # Trainer BLE power/cadence: ANT+ fills in if trainer BLE also silent
            if self._state.power_w == 0 and pwr_fresh and ant.power_w > 0:
                self._state.power_w = ant.power_w
            if self._state.cadence_rpm == 0 and pwr_fresh and ant.cadence_rpm > 0:
                self._state.cadence_rpm = ant.cadence_rpm
            # Extended ANT+ fields — always sync
            self._state.ant_active     = ant.active
            self._state.torque_eff_l   = ant.torque_eff_l
            self._state.torque_eff_r   = ant.torque_eff_r
            self._state.pedal_smooth_l = ant.pedal_smooth_l
            self._state.pedal_smooth_r = ant.pedal_smooth_r

    def _record(self, elapsed: int):
        if not self._latest:
            return
        ant = self._ant.data if self._ant else None
        ble_rr = self._latest.rr_intervals_ms[-1] if self._latest.rr_intervals_ms else 0
        # Freshness guards — same thresholds as _on_ble_data to keep snapshots consistent.
        now = time.monotonic()
        ant_hr_fresh  = (ant is not None and ant.last_hr_ts  > 0
                         and (now - ant.last_hr_ts)  < _ANT_STALE_SECS)
        ant_pwr_fresh = (ant is not None and ant.last_pwr_ts > 0
                         and (now - ant.last_pwr_ts) < _ANT_STALE_SECS)
        self._data.append(DataPoint(
            ts             = datetime.now(timezone.utc),
            elapsed_sec    = elapsed,
            speed_kmh      = self._latest.speed_kmh,
            incline_pct    = self._latest.incline_pct,
            # ANT+ primary for HR/RR/running cadence/stride; BLE fallback
            hr_bpm         = (ant.hr_bpm     if ant_hr_fresh  and ant.hr_bpm > 0
                              else self._latest.hr_bpm),
            rr_ms          = (ant.rr_ms      if ant_hr_fresh  and ant.rr_ms > 0 else ble_rr),
            cadence_spm    = (ant.cadence_spm if ant and ant.cadence_spm > 0
                              else self._latest.cadence_spm),
            stride_len_m   = (ant.stride_len_m if ant and ant.stride_len_m > 0
                              else self._latest.stride_len_m),
            distance_m     = self._session_distance_m,
            # BLE pedal meter primary for cycling power; ANT+ fallback when fresh
            power_w        = self._latest.power_w or (ant.power_w    if ant_pwr_fresh else 0),
            cadence_rpm    = self._latest.cadence_rpm or (ant.cadence_rpm if ant_pwr_fresh else 0),
            power_w_pm          = self._latest.power_w_pm,
            cadence_rpm_pm      = self._latest.cadence_rpm_pm,
            pedal_balance_pct   = self._latest.pedal_balance_pct,
            torque_eff_l        = ant.torque_eff_l   if ant else 0.0,
            torque_eff_r        = ant.torque_eff_r   if ant else 0.0,
            pedal_smooth_l      = ant.pedal_smooth_l if ant else 0.0,
            pedal_smooth_r      = ant.pedal_smooth_r if ant else 0.0,
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

        if self._ant:
            self._ant.stop()
            self._ant = None

        # Upload whatever data was collected, even if workout was stopped early
        if self._data and self._current_plan:
            self._state.status = "uploading"
            try:
                await self._upload(self._current_plan)
            except Exception as e:
                log.warning("Upload after early stop failed: %s", e)

        self._state.status = "idle"

    async def set_target_power(self, watts: int) -> bool:
        if not self._trainer_ble:
            raise RuntimeError("Trainer not connected")
        ok = await self._trainer_ble.set_target_power(watts)
        if ok:
            self._state.target_power_w = watts
        return ok

    async def _run(self, workout_id: str):
        plan_path = WORKOUTS_DIR / f"{workout_id}.json"
        if not plan_path.exists():
            self._state.status = "error"
            self._state.error = f"Workout not found: {workout_id}"
            return

        with plan_path.open() as fh:
            plan = json.load(fh)

        self._current_plan = plan
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
            # Start ANT+ in parallel with BLE (best-effort — continues if dongle absent)
            self._ant = ANTManager()
            if not self._ant.start():
                log.info("ANT+ not available — running BLE-only")
                self._ant = None

            self._trainer_ble = TrainerBLEManager(on_data=self._on_ble_data)
            await self._trainer_ble.connect()
            self._state.status = "ready"

            # Retry request_control + start up to 30s (trainer may need reconnect)
            for _attempt in range(15):
                try:
                    await self._trainer_ble.request_control()
                    break
                except RuntimeError:
                    log.warning("request_control failed — waiting 2s (attempt %d/15)", _attempt + 1)
                    await asyncio.sleep(2.0)
            else:
                raise RuntimeError("Trainer unavailable: request_control failed after 30s")

            for _attempt in range(5):
                try:
                    await self._trainer_ble.start()
                    break
                except RuntimeError:
                    await asyncio.sleep(2.0)

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
                    power = (self._latest.power_w_pm or self._latest.power_w)
                    if (cadence and cadence > 0) or power > 10:
                        log.info("Pedaling detected (cadence=%d rpm, power=%dW) — starting workout timer",
                                 cadence or 0, power)
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
                # Retry up to 45s to handle mid-workout trainer reconnect.
                # If still unavailable, continue without ERG control (record pedal data only).
                for _attempt in range(45):
                    try:
                        await self._trainer_ble.set_target_power(target_power)
                        break
                    except RuntimeError:
                        log.warning("Trainer not ready — waiting 1s (attempt %d/45)", _attempt + 1)
                        await asyncio.sleep(1.0)
                else:
                    log.warning("Could not set target power %dW after 45s — continuing without ERG", target_power)

                step_start = time.monotonic()
                while (elapsed := time.monotonic() - step_start) < duration:
                    self._state.elapsed_sec   = total_elapsed + int(elapsed)
                    self._state.remaining_sec = int(duration - elapsed)

                    if self._latest:
                        # Always calculate from speed — treadmill distance counter
                        # is not reset between sessions, so device-reported values
                        # carry over from previous use.
                        self._session_distance_m += self._latest.speed_kmh / 3.6
                        self._state.distance_m = self._session_distance_m

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
            if self._ant:
                self._ant.stop()
                self._ant = None

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
                        # Stale check: if no new FTMS packet for >3s, treat speed as 0
                        data_age = time.monotonic() - self._latest_ts
                        live_speed = self._latest.speed_kmh if data_age <= 3.0 else 0.0
                        if data_age > 3.0 and self._latest.speed_kmh > 0:
                            log.warning("Treadmill data stale (%.1fs) — zeroing speed", data_age)
                            self._latest.speed_kmh = 0.0
                            self._state.speed_kmh = 0.0
                        self._session_distance_m += live_speed / 3.6
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
