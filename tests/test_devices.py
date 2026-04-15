"""Tests for fitness_machine_mcp.devices — pure capability/role detection functions."""
from __future__ import annotations

import pytest

# Import only the pure functions — no BLE runtime needed
from fitness_machine_mcp.devices import _caps_from_services, _suggest_role

_SVC_FTMS          = "00001826-0000-1000-8000-00805f9b34fb"
_SVC_HEART_RATE    = "0000180d-0000-1000-8000-00805f9b34fb"
_SVC_CYCLING_POWER = "00001818-0000-1000-8000-00805f9b34fb"
_SVC_CSC           = "00001816-0000-1000-8000-00805f9b34fb"


# ── _caps_from_services ────────────────────────────────────────────────────────

class TestCapsFromServices:
    def test_empty_list(self):
        assert _caps_from_services([]) == []

    def test_ftms_only(self):
        caps = _caps_from_services([_SVC_FTMS])
        assert "ftms" in caps
        assert "hrm" not in caps

    def test_heart_rate_only(self):
        caps = _caps_from_services([_SVC_HEART_RATE])
        assert "hrm" in caps

    def test_cycling_power_only(self):
        caps = _caps_from_services([_SVC_CYCLING_POWER])
        assert "cycling_power" in caps

    def test_csc_only(self):
        caps = _caps_from_services([_SVC_CSC])
        assert "csc" in caps

    def test_ftms_and_cycling_power(self):
        caps = _caps_from_services([_SVC_FTMS, _SVC_CYCLING_POWER])
        assert "ftms" in caps
        assert "cycling_power" in caps

    def test_all_services(self):
        all_svcs = [_SVC_FTMS, _SVC_HEART_RATE, _SVC_CYCLING_POWER, _SVC_CSC]
        caps = _caps_from_services(all_svcs)
        assert set(caps) == {"ftms", "hrm", "cycling_power", "csc"}

    def test_case_insensitive(self):
        # UUIDs may come in uppercase from BLE scanner
        caps = _caps_from_services([_SVC_FTMS.upper()])
        assert "ftms" in caps

    def test_unknown_uuid_ignored(self):
        caps = _caps_from_services(["00001234-0000-1000-8000-00805f9b34fb"])
        assert caps == []


# ── _suggest_role ─────────────────────────────────────────────────────────────

class TestSuggestRole:
    def test_ftms_and_cycling_power_is_trainer(self):
        assert _suggest_role(["ftms", "cycling_power"]) == "trainer"

    def test_ftms_only_is_treadmill(self):
        assert _suggest_role(["ftms"]) == "treadmill"

    def test_hrm_only_is_hrm(self):
        assert _suggest_role(["hrm"]) == "hrm"

    def test_cycling_power_only_is_power_meter(self):
        assert _suggest_role(["cycling_power"]) == "power_meter"

    def test_csc_only_returns_none(self):
        assert _suggest_role(["csc"]) is None

    def test_empty_caps_returns_none(self):
        assert _suggest_role([]) is None

    def test_ftms_takes_priority_over_hrm(self):
        # FTMS + HRM → still treadmill (HRM could be built-in sensor)
        role = _suggest_role(["ftms", "hrm"])
        assert role == "treadmill"

    def test_trainer_priority_over_treadmill(self):
        # FTMS + cycling_power → trainer (not treadmill)
        role = _suggest_role(["ftms", "cycling_power", "csc"])
        assert role == "trainer"
