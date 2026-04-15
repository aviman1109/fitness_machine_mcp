"""Tests for fitness_machine_mcp.engine — workout file I/O (no BLE required)."""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


SAMPLE_PLAN = {
    "name": "Test Z2 Run",
    "sport": "running",
    "description": "Easy aerobic run",
    "incline_pct": 1.0,
    "steps": [
        {"name": "Warm Up", "duration_secs": 300, "speed_kmh": 8.0},
        {"name": "Z2 Run",  "duration_secs": 1800, "speed_kmh": 10.0},
        {"name": "Cool Down", "duration_secs": 300, "speed_kmh": 6.0},
    ],
}

CYCLING_PLAN = {
    "name": "Test ERG 45min",
    "sport": "cycling",
    "description": "Threshold intervals",
    "steps": [
        {"name": "Warm Up", "duration_secs": 600, "power_w": 140},
        {"name": "Threshold", "duration_secs": 1800, "power_w": 220},
        {"name": "Cool Down", "duration_secs": 600, "power_w": 100},
    ],
}


@pytest.fixture
def workout_dir(tmp_path):
    """Temporary workouts directory, patched into the engine module."""
    d = tmp_path / "workouts"
    d.mkdir()
    return d


@pytest.fixture
def engine(workout_dir):
    """WorkoutEngine with WORKOUTS_DIR pointed at the temp directory.

    We patch at the module level because list/get/save/delete_workout use
    the module-level WORKOUTS_DIR constant via the Path object.
    """
    import fitness_machine_mcp.engine as eng_mod
    original = eng_mod.WORKOUTS_DIR
    eng_mod.WORKOUTS_DIR = workout_dir
    # WorkoutEngine imports BLE/ANT managers at __init__ — mock them out
    with patch("fitness_machine_mcp.engine.BLEManager"), \
         patch("fitness_machine_mcp.engine.TrainerBLEManager"), \
         patch("fitness_machine_mcp.engine.ANTManager"):
        from fitness_machine_mcp.engine import WorkoutEngine
        e = WorkoutEngine()
    yield e
    eng_mod.WORKOUTS_DIR = original


# ── list_workouts ─────────────────────────────────────────────────────────────

class TestListWorkouts:
    def test_empty_dir_returns_empty_list(self, engine):
        assert engine.list_workouts() == []

    def test_returns_one_workout(self, engine, workout_dir):
        (workout_dir / "test_run.json").write_text(json.dumps(SAMPLE_PLAN))
        workouts = engine.list_workouts()
        assert len(workouts) == 1
        assert workouts[0]["id"] == "test_run"
        assert workouts[0]["name"] == "Test Z2 Run"

    def test_duration_secs_is_sum_of_steps(self, engine, workout_dir):
        (workout_dir / "test_run.json").write_text(json.dumps(SAMPLE_PLAN))
        workouts = engine.list_workouts()
        assert workouts[0]["duration_secs"] == 300 + 1800 + 300

    def test_multiple_workouts_sorted_alphabetically(self, engine, workout_dir):
        for name in ["zzz_plan", "aaa_plan", "mmm_plan"]:
            plan = {**SAMPLE_PLAN, "name": name}
            (workout_dir / f"{name}.json").write_text(json.dumps(plan))
        workouts = engine.list_workouts()
        ids = [w["id"] for w in workouts]
        assert ids == sorted(ids)

    def test_ignores_invalid_json(self, engine, workout_dir):
        (workout_dir / "broken.json").write_text("not json {")
        (workout_dir / "good.json").write_text(json.dumps(SAMPLE_PLAN))
        workouts = engine.list_workouts()
        assert len(workouts) == 1
        assert workouts[0]["id"] == "good"


# ── get_workout ───────────────────────────────────────────────────────────────

class TestGetWorkout:
    def test_returns_plan_dict(self, engine, workout_dir):
        (workout_dir / "run.json").write_text(json.dumps(SAMPLE_PLAN))
        result = engine.get_workout("run")
        assert result["name"] == "Test Z2 Run"
        assert len(result["steps"]) == 3

    def test_returns_none_when_not_found(self, engine):
        assert engine.get_workout("nonexistent") is None


# ── save_workout ──────────────────────────────────────────────────────────────

class TestSaveWorkout:
    def test_creates_file(self, engine, workout_dir):
        engine.save_workout("new_plan", SAMPLE_PLAN)
        path = workout_dir / "new_plan.json"
        assert path.exists()

    def test_saved_content_is_valid_json(self, engine, workout_dir):
        engine.save_workout("new_plan", SAMPLE_PLAN)
        content = json.loads((workout_dir / "new_plan.json").read_text())
        assert content["name"] == SAMPLE_PLAN["name"]

    def test_overwrites_existing(self, engine, workout_dir):
        engine.save_workout("plan", SAMPLE_PLAN)
        updated = {**SAMPLE_PLAN, "name": "Updated"}
        engine.save_workout("plan", updated)
        content = json.loads((workout_dir / "plan.json").read_text())
        assert content["name"] == "Updated"

    def test_roundtrip_list_get(self, engine, workout_dir):
        engine.save_workout("roundtrip", SAMPLE_PLAN)
        retrieved = engine.get_workout("roundtrip")
        assert retrieved == SAMPLE_PLAN

    def test_roundtrip_cycling_plan(self, engine, workout_dir):
        engine.save_workout("cycling_plan", CYCLING_PLAN)
        retrieved = engine.get_workout("cycling_plan")
        assert retrieved == CYCLING_PLAN


# ── delete_workout ─────────────────────────────────────────────────────────────

class TestDeleteWorkout:
    def test_deletes_existing_file(self, engine, workout_dir):
        (workout_dir / "del_me.json").write_text(json.dumps(SAMPLE_PLAN))
        result = engine.delete_workout("del_me")
        assert result is True
        assert not (workout_dir / "del_me.json").exists()

    def test_returns_false_when_not_found(self, engine):
        result = engine.delete_workout("ghost")
        assert result is False

    def test_gone_after_delete(self, engine, workout_dir):
        engine.save_workout("temp", SAMPLE_PLAN)
        engine.delete_workout("temp")
        assert engine.get_workout("temp") is None
