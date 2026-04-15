"""
Treadmill Workout Server
========================
HTTP server that controls the ADIDAS 1380 treadmill via BLE FTMS.

Usage:
  cd ~/treadmill
  ~/.ble-venv/bin/python server.py

Endpoints:
  GET  /workouts          — list available workout plans
  POST /start/{workout_id}— start a workout
  GET  /status            — current state
  POST /stop              — emergency stop
"""
from __future__ import annotations
import asyncio, logging, sys
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
import uvicorn

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("server")

from engine import WorkoutEngine

engine = WorkoutEngine()


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Treadmill server ready on http://0.0.0.0:8765")
    yield
    log.info("Shutting down")
    await engine.emergency_stop()


app = FastAPI(title="Treadmill Controller", lifespan=lifespan)


@app.get("/workouts")
async def list_workouts():
    return engine.list_workouts()


@app.post("/start/{workout_id}")
async def start_workout(workout_id: str):
    state = engine.state
    if state.status in ("running", "connecting"):
        raise HTTPException(409, f"Workout already active: {state.workout_name}")

    ok = engine.start(workout_id)
    if not ok:
        raise HTTPException(400, "Could not start workout")

    return {"started": workout_id, "message": "插好安全鑰匙，課表開始執行"}


@app.get("/status")
async def get_status():
    s = engine.state
    return {
        "status":        s.status,
        "workout":       s.workout_name,
        "step":          f"{s.step_name} ({s.step_index+1}/{s.step_total})" if s.step_total else "",
        "elapsed_sec":   s.elapsed_sec,
        "remaining_sec": s.remaining_sec,
        "speed_kmh":     round(s.speed_kmh, 1),
        "hr_bpm":        s.hr_bpm,
        "cadence_spm":   s.cadence_spm,
        "distance_m":    round(s.distance_m, 1),
        "garmin_activity_id": s.garmin_activity_id,
        "error":         s.error,
    }


@app.post("/stop")
async def emergency_stop():
    await engine.emergency_stop()
    return {"stopped": True}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8765, log_config=None)
