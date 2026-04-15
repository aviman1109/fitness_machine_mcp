"""Tests for fitness_machine_mcp.tcx — training effect calculation and TCX generation."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import pytest

from fitness_machine_mcp.tcx import build_tcx, calculate_training_effect


# ── DataPoint stub ─────────────────────────────────────────────────────────────
# We use a lightweight stand-in so this test module has no BLE/bleak dependency.

from dataclasses import dataclass, field


@dataclass
class _DP:
    """Minimal DataPoint stub for TCX tests."""
    ts: datetime
    elapsed_sec: int
    speed_kmh: float
    incline_pct: float
    hr_bpm: int
    cadence_spm: int
    stride_len_m: float
    distance_m: float
    power_w: int = 0
    cadence_rpm: int = 0
    rr_ms: int = 0
    power_w_pm: int = 0
    cadence_rpm_pm: int = 0
    pedal_balance_pct: float = 0.0
    torque_eff_l: float = 0.0
    torque_eff_r: float = 0.0
    pedal_smooth_l: float = 0.0
    pedal_smooth_r: float = 0.0


def _run_points(n_secs: int, hr: int, speed_kmh: float = 10.0) -> list[_DP]:
    base = datetime(2026, 4, 13, 6, 0, 0, tzinfo=timezone.utc)
    return [
        _DP(
            ts=base,
            elapsed_sec=i,
            speed_kmh=speed_kmh,
            incline_pct=0.0,
            hr_bpm=hr,
            cadence_spm=180,
            stride_len_m=1.0,
            distance_m=float(i) * speed_kmh / 3.6,
        )
        for i in range(n_secs)
    ]


# ── calculate_training_effect ──────────────────────────────────────────────────

class TestCalculateTrainingEffect:
    def test_empty_data_returns_zero(self):
        aerobic, anaerobic = calculate_training_effect([])
        assert aerobic == 0.0
        assert anaerobic == 0.0

    def test_z1_hr_produces_low_te(self):
        # Z1 < 69% LTHR (173) ≈ < 119 bpm — aerobic TE should be minimal
        pts = _run_points(3600, hr=100)  # 1 hour @ Z1
        aerobic, anaerobic = calculate_training_effect(pts)
        assert 1.0 <= aerobic <= 2.5
        assert anaerobic == 0.0

    def test_z2_hr_60min_produces_moderate_aerobic(self):
        # Z2: 69-83% LTHR ≈ 119-143 bpm
        pts = _run_points(3600, hr=130)
        aerobic, anaerobic = calculate_training_effect(pts)
        assert 2.0 <= aerobic <= 4.5
        assert anaerobic == 0.0

    def test_z4_hr_produces_high_aerobic_and_anaerobic(self):
        # Z4: 95-105% LTHR ≈ 164-182 bpm
        pts = _run_points(3600, hr=175)
        aerobic, anaerobic = calculate_training_effect(pts)
        assert aerobic >= 3.0
        assert anaerobic > 0.0

    def test_aerobic_te_capped_at_5(self):
        # Long Z5 session — should never exceed 5.0
        pts = _run_points(7200, hr=190)
        aerobic, _ = calculate_training_effect(pts)
        assert aerobic <= 5.0

    def test_anaerobic_te_capped_at_3(self):
        pts = _run_points(7200, hr=190)
        _, anaerobic = calculate_training_effect(pts)
        assert anaerobic <= 3.0

    def test_zero_hr_points_skipped(self):
        # Points with hr=0 are skipped → TRIMP stays 0 → formula yields 1.0 baseline (not 0.0)
        # 0.0 is only returned for truly empty data (handled by test_empty_data_returns_zero)
        pts = _run_points(100, hr=0)
        aerobic, anaerobic = calculate_training_effect(pts)
        assert aerobic == 1.0   # baseline for 0 TRIMP
        assert anaerobic == 0.0


# ── build_tcx ─────────────────────────────────────────────────────────────────

class TestBuildTcx:
    def _base_plan(self, sport="running"):
        return {"sport": sport, "name": "Test Workout"}

    def _base_data(self):
        return _run_points(60, hr=145)

    def test_returns_bytes(self):
        result = build_tcx(self._base_plan(), self._base_data(),
                           datetime(2026, 4, 13, 6, 0, 0, tzinfo=timezone.utc))
        assert isinstance(result, bytes)

    def test_valid_xml(self):
        result = build_tcx(self._base_plan(), self._base_data(),
                           datetime(2026, 4, 13, 6, 0, 0, tzinfo=timezone.utc))
        # Should not raise
        ET.fromstring(result)

    def test_sport_running_maps_to_Running(self):
        xml_bytes = build_tcx(self._base_plan("running"), self._base_data(),
                              datetime(2026, 4, 13, 6, 0, 0, tzinfo=timezone.utc))
        root = ET.fromstring(xml_bytes)
        ns = {"tc": "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2"}
        activity = root.find(".//tc:Activity", ns)
        assert activity is not None
        assert activity.get("Sport") == "Running"

    def test_sport_cycling_maps_to_Biking(self):
        xml_bytes = build_tcx(self._base_plan("cycling"), self._base_data(),
                              datetime(2026, 4, 13, 6, 0, 0, tzinfo=timezone.utc))
        root = ET.fromstring(xml_bytes)
        ns = {"tc": "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2"}
        activity = root.find(".//tc:Activity", ns)
        assert activity is not None
        assert activity.get("Sport") == "Biking"

    def test_creator_uses_real_device_when_provided(self):
        garmin_device = {"product_id": 4312, "unit_id": 3961234567, "name": "Forerunner 965"}
        xml_bytes = build_tcx(self._base_plan(), self._base_data(),
                              datetime(2026, 4, 13, 6, 0, 0, tzinfo=timezone.utc),
                              garmin_device=garmin_device)
        xml_str = xml_bytes.decode()
        assert "Forerunner 965" in xml_str
        assert "4312" in xml_str
        assert "3961234567" in xml_str

    def test_creator_uses_fallback_when_no_device(self):
        xml_bytes = build_tcx(self._base_plan(), self._base_data(),
                              datetime(2026, 4, 13, 6, 0, 0, tzinfo=timezone.utc),
                              garmin_device=None)
        xml_str = xml_bytes.decode()
        assert "ADIDAS 1380 Treadmill" in xml_str

    def test_creator_cycling_fallback_device(self):
        xml_bytes = build_tcx(self._base_plan("cycling"), self._base_data(),
                              datetime(2026, 4, 13, 6, 0, 0, tzinfo=timezone.utc),
                              garmin_device=None)
        xml_str = xml_bytes.decode()
        assert "eHuntSun T-SoX Trainer" in xml_str

    def test_trackpoints_match_data_count(self):
        data = self._base_data()
        xml_bytes = build_tcx(self._base_plan(), data,
                              datetime(2026, 4, 13, 6, 0, 0, tzinfo=timezone.utc))
        root = ET.fromstring(xml_bytes)
        ns = {"tc": "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2"}
        trackpoints = root.findall(".//tc:Trackpoint", ns)
        assert len(trackpoints) == len(data)

    def test_lap_total_time_correct(self):
        data = _run_points(300, hr=145)
        xml_bytes = build_tcx(self._base_plan(), data,
                              datetime(2026, 4, 13, 6, 0, 0, tzinfo=timezone.utc))
        root = ET.fromstring(xml_bytes)
        ns = {"tc": "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2"}
        total_time = root.find(".//tc:TotalTimeSeconds", ns)
        assert total_time is not None
        assert total_time.text == "299"  # elapsed_sec of last point (0-indexed, 300 pts → last is 299)
