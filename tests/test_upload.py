"""Tests for fitness_machine_mcp.upload — get_primary_garmin_device() with mocked garth.

New two-step lookup:
  1. GET recent activities → extract deviceId
  2. GET /device-service/deviceservice/device-info/{deviceId} → unitId + name
"""
from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest


SAMPLE_ACTIVITIES = [
    {"activityId": 12345, "activityName": "Indoor Cycling", "deviceId": 3464969397},
    {"activityId": 12344, "activityName": "Treadmill Run",  "deviceId": 3464969397},
]

SAMPLE_DEVICE_INFO = {
    "productDisplayName": "Descent MK3 - 43 mm",
    "baseDeviceDTO": {
        "unitId": 3464969397,
        "deviceId": 3464969397,
        "displayName": "Descent Mk3 43mm",
        "primaryActivityTrackerIndicator": True,
    },
}


class TestGetPrimaryGarminDevice:
    def setup_method(self):
        import fitness_machine_mcp.upload as upload_mod
        upload_mod._cached_device = None

    def _mock_client(self, activities=None, device_info=None, side_effect=None):
        client = MagicMock()
        if side_effect:
            client.connectapi.side_effect = side_effect
        else:
            def _connectapi(path, **kwargs):
                if "activities/search" in path:
                    return activities if activities is not None else []
                if "device-info" in path:
                    return device_info or {}
                return None
            client.connectapi.side_effect = _connectapi
        return client

    def test_returns_device_info_when_found(self):
        import fitness_machine_mcp.upload as upload_mod
        mock_client = self._mock_client(SAMPLE_ACTIVITIES, SAMPLE_DEVICE_INFO)
        with patch.object(upload_mod, "_get_client", return_value=mock_client):
            result = upload_mod.get_primary_garmin_device()
        assert result["unit_id"] == 3464969397
        assert result["product_id"] == 3464969397  # unitId used as stand-in
        assert result["name"] == "Descent Mk3 43mm"

    def test_returns_empty_dict_when_no_activities(self):
        import fitness_machine_mcp.upload as upload_mod
        mock_client = self._mock_client(activities=[])
        with patch.object(upload_mod, "_get_client", return_value=mock_client):
            result = upload_mod.get_primary_garmin_device()
        assert result == {}

    def test_returns_empty_dict_when_activities_have_no_device_id(self):
        import fitness_machine_mcp.upload as upload_mod
        acts = [{"activityId": 1, "activityName": "Run", "deviceId": None}]
        mock_client = self._mock_client(activities=acts)
        with patch.object(upload_mod, "_get_client", return_value=mock_client):
            result = upload_mod.get_primary_garmin_device()
        assert result == {}

    def test_returns_empty_dict_on_exception(self):
        import fitness_machine_mcp.upload as upload_mod
        mock_client = self._mock_client(side_effect=Exception("network error"))
        with patch.object(upload_mod, "_get_client", return_value=mock_client):
            result = upload_mod.get_primary_garmin_device()
        assert result == {}

    def test_caches_result_on_second_call(self):
        import fitness_machine_mcp.upload as upload_mod
        mock_client = self._mock_client(SAMPLE_ACTIVITIES, SAMPLE_DEVICE_INFO)
        with patch.object(upload_mod, "_get_client", return_value=mock_client):
            first = upload_mod.get_primary_garmin_device()
            second = upload_mod.get_primary_garmin_device()
        # connectapi called only once across both get_primary_garmin_device() calls
        assert mock_client.connectapi.call_count == 2  # activities + device-info, then cache
        assert first == second

    def test_uses_first_device_id_from_activities(self):
        """Uses the deviceId from the most recent activity."""
        import fitness_machine_mcp.upload as upload_mod
        acts = [
            {"activityId": 2, "deviceId": 9999},
            {"activityId": 1, "deviceId": 1111},
        ]
        device_info = {
            "baseDeviceDTO": {"unitId": 9999, "displayName": "Primary Watch"},
        }
        mock_client = self._mock_client(acts, device_info)
        with patch.object(upload_mod, "_get_client", return_value=mock_client):
            result = upload_mod.get_primary_garmin_device()
        assert result["unit_id"] == 9999

    def test_falls_back_to_productDisplayName(self):
        """Uses productDisplayName when baseDeviceDTO.displayName is missing."""
        import fitness_machine_mcp.upload as upload_mod
        device_info = {
            "productDisplayName": "Forerunner 965",
            "baseDeviceDTO": {"unitId": 555},
        }
        mock_client = self._mock_client(SAMPLE_ACTIVITIES, device_info)
        with patch.object(upload_mod, "_get_client", return_value=mock_client):
            result = upload_mod.get_primary_garmin_device()
        assert result["name"] == "Forerunner 965"
