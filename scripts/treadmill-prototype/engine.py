"""Workout execution engine."""
from __future__ import annotations
import asyncio, json, logging, time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from ble import BLEManager, LiveData

log = logging.getLogger("engine")
WORKOUTS_DIR = Path(__file__).parent / "workouts"


@dataclass
class DataPoint:
    ts: datetime
    elapsed_sec: int
    speed_kmh: float
    incline_pct: float
    hr_bpm: int
    cadence_spm: int
    stride_len_m: float
    distance_m: float   # cumulative for this session


@dataclass
class WorkoutState:
    status: str = "idle"            # idle|connecting|ready|running|done|error
    workout_name: str = ""
    step_name: str = ""
    step_index: int = 0
    step_total: int = 0
    elapsed_sec: int = 0
    remaining_sec: int = 0
    speed_kmh: float = 0.0
    hr_bpm: int = 0
    cadence_spm: int = 0
    distance_m: float = 0.0
    garmin_activity_id: Optional[int] = None
    error: str = ""


class WorkoutEngine:
    def __init__(self):
        self._state = WorkoutState()
        self._ble: Optional[BLEManager] = None
        self._data: list[DataPoint] = []
        self._start_time: Optional[datetime] = None
        self._session_distance_m: float = 0.0
        self._last_distance_m: float = 0.0   # treadmill odometer at session start
        self._latest: Optional[LiveData] = None
        self._task: Optional[asyncio.Task] = None

    @property
    def state(self) -> WorkoutState:
        return self._state

    def list_workouts(self) -> list[dict]:
        plans = []
        for f in sorted(WORKOUTS_DIR.glob("*.json")):
            with f.open() as fh:
                p = json.load(fh)
            total = sum(s["duration_secs"] for s in p["steps"])
            plans.append({"id": f.stem, "name": p["name"],
                           "description": p.get("description", ""),
                           "duration_secs": total})
        return plans

    def _on_ble_data(self, live: LiveData):
        self._latest = live
        self._state.speed_kmh   = live.speed_kmh
        self._state.hr_bpm      = live.hr_bpm
        self._state.cadence_spm = live.cadence_spm

    def _record(self, elapsed: int):
        if not self._latest:
            return
        # Estimate session distance from speed (more reliable than treadmill odometer)
        self._data.append(DataPoint(
            ts           = datetime.now(timezone.utc),
            elapsed_sec  = elapsed,
            speed_kmh    = self._latest.speed_kmh,
            incline_pct  = self._latest.incline_pct,
            hr_bpm       = self._latest.hr_bpm,
            cadence_spm  = self._latest.cadence_spm,
            stride_len_m = self._latest.stride_len_m,
            distance_m   = self._session_distance_m,
        ))

    def start(self, workout_id: str) -> bool:
        if self._state.status not in ("idle", "done", "error"):
            return False
        self._task = asyncio.create_task(self._run(workout_id))
        return True

    async def emergency_stop(self):
        if self._ble:
            await self._ble.stop()
        if self._task:
            self._task.cancel()
        self._state.status = "idle"

    async def _run(self, workout_id: str):
        plan_path = WORKOUTS_DIR / f"{workout_id}.json"
        if not plan_path.exists():
            self._state.status = "error"
            self._state.error = f"Workout not found: {workout_id}"
            return

        with plan_path.open() as fh:
            plan = json.load(fh)

        steps = plan["steps"]
        incline = plan.get("incline_pct", 1.0)
        self._state.workout_name = plan["name"]
        self._state.step_total   = len(steps)
        self._state.status       = "connecting"
        self._data = []
        self._session_distance_m = 0.0

        try:
            # ── Connect ──────────────────────────────────────────────────
            self._ble = BLEManager(on_data=self._on_ble_data)
            await self._ble.connect()
            self._state.status = "ready"

            await self._ble.request_control()
            await self._ble.set_incline(incline)
            await asyncio.sleep(1)

            # Wait for user to press start on treadmill (or send start command)
            # Send start command — safety key must be inserted for belt to move
            await self._ble.start()
            await asyncio.sleep(2)

            self._state.status = "running"
            self._start_time = datetime.now(timezone.utc)
            total_elapsed = 0

            # ── Execute steps ─────────────────────────────────────────────
            for i, step in enumerate(steps):
                self._state.step_index = i
                self._state.step_name  = step["name"]
                target_speed = step["speed_kmh"]
                duration     = step["duration_secs"]

                log.info("Step %d/%d: %s — %.1f km/h for %ds",
                         i+1, len(steps), step["name"], target_speed, duration)

                # Ramp to target speed over 8s
                await self._ble.ramp_speed(target_speed, duration_secs=8.0)

                # Hold for remaining duration, recording every second
                step_start = time.monotonic()
                while (elapsed := time.monotonic() - step_start) < duration:
                    self._state.elapsed_sec   = total_elapsed + int(elapsed)
                    self._state.remaining_sec = int(duration - elapsed)

                    # Accumulate distance (speed-based, 1s interval)
                    if self._latest:
                        self._session_distance_m += self._latest.speed_kmh / 3.6
                    self._state.distance_m = self._session_distance_m

                    self._record(total_elapsed + int(elapsed))
                    await asyncio.sleep(1.0)

                total_elapsed += duration

            # ── Stop ─────────────────────────────────────────────────────
            log.info("Workout complete. Stopping treadmill.")
            await self._ble.stop()
            self._state.status = "done"

        except asyncio.CancelledError:
            log.info("Workout cancelled")
            if self._ble:
                await self._ble.stop()
            self._state.status = "idle"
            return
        except Exception as e:
            log.exception("Workout error: %s", e)
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

        # ── Upload ───────────────────────────────────────────────────────
        if self._data and self._state.status == "done":
            try:
                from tcx import build_tcx
                from upload import upload_activity

                tcx_path = Path(f"/tmp/treadmill_{datetime.now().strftime('%Y%m%d_%H%M%S')}.tcx")
                tcx_bytes = build_tcx(plan, self._data, self._start_time)
                tcx_path.write_bytes(tcx_bytes)
                log.info("TCX written: %s (%d bytes)", tcx_path, len(tcx_bytes))

                activity_id = await asyncio.get_event_loop().run_in_executor(
                    None, upload_activity, str(tcx_path)
                )
                self._state.garmin_activity_id = activity_id
                log.info("Uploaded to Garmin. Activity ID: %s", activity_id)
            except Exception as e:
                log.error("Upload failed: %s", e)
                self._state.error = f"Upload failed: {e}"
