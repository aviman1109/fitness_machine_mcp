"""Tests for server.py tool logic — create_workout and delete_workout validation.

Tests the tool functions directly (no HTTP, no BLE needed).
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_engine(status="idle", workout_name=""):
    from fitness_machine_mcp.engine import WorkoutState
    state = WorkoutState(status=status, workout_name=workout_name)
    engine = MagicMock()
    engine.state = state
    engine.get_workout = MagicMock(return_value=None)
    engine.save_workout = MagicMock()
    engine.delete_workout = MagicMock(return_value=True)
    return engine


def _import_tools(mock_engine):
    """Import server module with engine replaced by a mock."""
    import fitness_machine_mcp.server as srv
    orig = srv.engine
    srv.engine = mock_engine
    return srv, orig


# ── create_workout ─────────────────────────────────────────────────────────────

class TestCreateWorkout:
    def _run(self, mock_engine, **kwargs):
        srv, orig = _import_tools(mock_engine)
        try:
            return srv.create_workout(**kwargs)
        finally:
            srv.engine = orig

    def _cycling_step(self):
        from fitness_machine_mcp.server import WorkoutStep
        return WorkoutStep(name="Z2", duration_secs=3600, power_w=180)

    def _running_step(self):
        from fitness_machine_mcp.server import WorkoutStep
        return WorkoutStep(name="Z2", duration_secs=3600, speed_kmh=10.0)

    def test_cycling_workout_created(self):
        engine = _make_engine()
        result = self._run(engine,
            workout_id="test_cycling", name="Test", steps=[self._cycling_step()],
            sport="cycling")
        assert result["ok"] is True
        assert result["sport"] == "cycling"
        engine.save_workout.assert_called_once()

    def test_running_workout_created(self):
        engine = _make_engine()
        result = self._run(engine,
            workout_id="test_run", name="Test Run", steps=[self._running_step()],
            sport="running")
        assert result["ok"] is True
        assert result["sport"] == "running"
        engine.save_workout.assert_called_once()

    def test_running_workout_with_incline(self):
        engine = _make_engine()
        result = self._run(engine,
            workout_id="test_run", name="Hill Run", steps=[self._running_step()],
            sport="running", incline_pct=1.0)
        assert result["ok"] is True
        # Check that incline_pct was saved in the plan
        call_args = engine.save_workout.call_args
        saved_plan = call_args[0][1]
        assert saved_plan.get("incline_pct") == 1.0

    def test_invalid_sport_rejected(self):
        engine = _make_engine()
        result = self._run(engine,
            workout_id="bad_sport", name="Bad", steps=[self._cycling_step()],
            sport="swimming")
        assert result["ok"] is False
        assert "sport" in result["error"].lower()

    def test_invalid_workout_id_rejected(self):
        engine = _make_engine()
        result = self._run(engine,
            workout_id="has spaces!", name="Bad ID", steps=[self._cycling_step()],
            sport="cycling")
        assert result["ok"] is False
        assert "workout_id" in result["error"].lower()

    def test_empty_steps_rejected(self):
        engine = _make_engine()
        result = self._run(engine,
            workout_id="empty", name="Empty", steps=[], sport="cycling")
        assert result["ok"] is False

    def test_cycling_step_missing_power_rejected(self):
        from fitness_machine_mcp.server import WorkoutStep
        engine = _make_engine()
        step_no_power = WorkoutStep(name="No power", duration_secs=600)
        result = self._run(engine,
            workout_id="bad_step", name="Bad Step", steps=[step_no_power],
            sport="cycling")
        assert result["ok"] is False
        assert "power_w" in result["error"]

    def test_cycling_step_power_out_of_range_rejected(self):
        from fitness_machine_mcp.server import WorkoutStep
        engine = _make_engine()
        step_too_high = WorkoutStep(name="Too high", duration_secs=600, power_w=9999)
        result = self._run(engine,
            workout_id="bad_power", name="Bad", steps=[step_too_high],
            sport="cycling")
        assert result["ok"] is False

    def test_step_duration_too_long_rejected(self):
        from fitness_machine_mcp.server import WorkoutStep
        engine = _make_engine()
        long_step = WorkoutStep(name="Too long", duration_secs=99999, power_w=150)
        result = self._run(engine,
            workout_id="too_long", name="Too Long", steps=[long_step],
            sport="cycling")
        assert result["ok"] is False

    def test_total_duration_in_response(self):
        from fitness_machine_mcp.server import WorkoutStep
        engine = _make_engine()
        steps = [
            WorkoutStep(name="A", duration_secs=600, power_w=140),
            WorkoutStep(name="B", duration_secs=1800, power_w=200),
        ]
        result = self._run(engine,
            workout_id="duration_test", name="Test", steps=steps, sport="cycling")
        assert result["total_secs"] == 2400

    def test_running_step_without_power_ok(self):
        """Running steps don't require power_w."""
        from fitness_machine_mcp.server import WorkoutStep
        engine = _make_engine()
        step = WorkoutStep(name="Easy run", duration_secs=1800)  # no speed_kmh, no power_w
        result = self._run(engine,
            workout_id="bare_run", name="Bare Run", steps=[step], sport="running")
        assert result["ok"] is True


# ── delete_workout ─────────────────────────────────────────────────────────────

class TestDeleteWorkout:
    def _run(self, mock_engine, workout_id):
        srv, orig = _import_tools(mock_engine)
        try:
            return srv.delete_workout(workout_id=workout_id)
        finally:
            srv.engine = orig

    def test_deletes_idle_workout(self):
        engine = _make_engine(status="idle")
        engine.get_workout = MagicMock(return_value={"name": "Test Plan"})
        engine.delete_workout = MagicMock(return_value=True)
        result = self._run(engine, "some_plan")
        assert result["ok"] is True

    def test_not_found_returns_error(self):
        engine = _make_engine()
        engine.get_workout = MagicMock(return_value=None)
        result = self._run(engine, "ghost")
        assert result["ok"] is False
        assert "not found" in result["error"].lower()

    def test_cannot_delete_currently_running_workout(self):
        engine = _make_engine(status="running", workout_name="Z2 Run")
        engine.get_workout = MagicMock(return_value={"name": "Z2 Run"})
        result = self._run(engine, "z2_run")
        assert result["ok"] is False
        assert "running" in result["error"].lower()

    def test_can_delete_different_workout_while_another_runs(self):
        """A different (non-active) workout can be deleted while something else runs."""
        engine = _make_engine(status="running", workout_name="Active Ride")
        engine.get_workout = MagicMock(return_value={"name": "Old Run Plan"})
        engine.delete_workout = MagicMock(return_value=True)
        result = self._run(engine, "old_run")
        assert result["ok"] is True

    def test_cannot_delete_workout_during_connecting_phase(self):
        engine = _make_engine(status="connecting", workout_name="ERG Test")
        engine.get_workout = MagicMock(return_value={"name": "ERG Test"})
        result = self._run(engine, "erg_test")
        assert result["ok"] is False
