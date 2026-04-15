"""Upload activity TCX to Garmin Connect via garth."""
from __future__ import annotations
import logging
import os
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore", category=DeprecationWarning)
import garth  # noqa: E402

log = logging.getLogger("fitness_machine.upload")

TOKEN_PATH = Path(os.environ.get(
    "GARMIN_TOKEN_PATH",
    str(Path.home() / ".garmin-tokens")
))

_POLL_RETRIES    = 10   # how many times to check for the new activity
_POLL_INTERVAL_S = 2    # seconds between checks

# Cached primary Garmin device info — populated once on first upload.
# Used in TCX Creator so Garmin treats the activity as coming from the user's
# real watch, which enables Training Load / Physio TrueUp on the watch.
_cached_device: dict | None = None


def get_primary_garmin_device() -> dict:
    """Return the user's primary Garmin device info (product_id, unit_id, name).

    Two-step lookup (the old /device-service/deviceservice/devices endpoint is gone):
      1. Pull deviceId from a recent activity.
      2. Fetch device details via /device-service/deviceservice/device-info/{deviceId}.

    Falls back to an empty dict on failure so callers handle gracefully.

    Example return value:
        {"product_id": 3464969397, "unit_id": 3464969397, "name": "Descent Mk3 43mm"}
    """
    global _cached_device
    if _cached_device is not None:
        return _cached_device

    client = _get_client()
    try:
        # Step 1: grab device IDs seen in recent activities (primary tracker appears first)
        acts = client.connectapi(
            "/activitylist-service/activities/search/activities",
            params={"limit": 10, "start": 0},
        ) or []
        device_ids = [a["deviceId"] for a in acts if a.get("deviceId")]
        if not device_ids:
            log.warning("No device ID found in recent activities — Training Load will not update on watch")
            _cached_device = {}
            return _cached_device

        # Step 2: fetch device details for the first (most recent) device
        device_id = device_ids[0]
        info = client.connectapi(f"/device-service/deviceservice/device-info/{device_id}")
        base = info.get("baseDeviceDTO", {})
        uid  = base.get("unitId") or info.get("unitId") or device_id
        name = (base.get("displayName") or info.get("productDisplayName") or
                info.get("displayName") or str(device_id))

        # product_id: Garmin's new API doesn't expose a numeric productId, so we
        # use unitId as a stand-in — the UnitId is the field Garmin actually uses
        # to match an upload to a watch for Physio TrueUp Training Load sync.
        _cached_device = {"product_id": int(uid), "unit_id": int(uid), "name": str(name)}
        log.info("Primary Garmin device: %s (unit_id=%s)", name, uid)
        return _cached_device

    except Exception as e:
        log.warning("Could not fetch Garmin device info: %s", e)

    _cached_device = {}
    return _cached_device


def _get_client() -> garth.Client:
    garth.resume(str(TOKEN_PATH))
    return garth.client


def upload_activity(tcx_path: str) -> int | None:
    """Upload a TCX file to Garmin Connect. Returns activity ID or None.

    Garmin's upload API is asynchronous — the activity ID is not returned in
    the upload response. We poll the recent-activities list until the new entry
    appears (up to POLL_RETRIES × POLL_INTERVAL_S seconds).
    """
    client = _get_client()

    with open(tcx_path, "rb") as fp:
        resp = client.upload(fp)

    log.info("Upload accepted: uploadId=%s file=%s",
             resp.get("detailedImportResult", {}).get("uploadId"),
             Path(tcx_path).name)

    failures = resp.get("detailedImportResult", {}).get("failures", [])
    if failures:
        log.error("Upload rejected by Garmin: %s", failures)
        return None

    # Snapshot existing activity IDs before polling so we can detect the new one
    try:
        before = {
            a["activityId"]
            for a in (client.connectapi(
                "/activitylist-service/activities/search/activities",
                params={"limit": 10, "start": 0},
            ) or [])
        }
    except Exception:
        before = set()

    # Poll until a new activity ID appears
    for attempt in range(1, _POLL_RETRIES + 1):
        time.sleep(_POLL_INTERVAL_S)
        try:
            acts = client.connectapi(
                "/activitylist-service/activities/search/activities",
                params={"limit": 10, "start": 0},
            ) or []
            new_ids = [a["activityId"] for a in acts if a["activityId"] not in before]
            if new_ids:
                activity_id = new_ids[0]
                name = next((a["activityName"] for a in acts if a["activityId"] == activity_id), "?")
                log.info("Poll %d/%d — new activity: %s '%s'", attempt, _POLL_RETRIES, activity_id, name)
                return activity_id
            log.info("Poll %d/%d — activity not yet visible", attempt, _POLL_RETRIES)
        except Exception as e:
            log.warning("Poll %d/%d failed: %s", attempt, _POLL_RETRIES, e)

    log.warning("Could not retrieve activity ID after %d polls", _POLL_RETRIES)
    return None


def set_training_effect(activity_id: int, aerobic_te: float, anaerobic_te: float) -> bool:
    """Inject Training Effect into an uploaded Garmin Connect activity.

    TCX uploads bypass on-device Training Effect calculation, so Garmin Connect
    shows the activity with no TE and it doesn't contribute to Training Load on
    the watch.  This function writes the pre-calculated TE values back via the
    activity-service PUT endpoint so training load is correctly accounted for.

    Garmin TE scale: 0.0–5.0 (aerobic), 0.0–3.0 (anaerobic).
    Values are rounded to 1 decimal place.
    """
    client = _get_client()
    try:
        client.connectapi(
            f"/activity-service/activity/{activity_id}",
            method="PUT",
            json={
                "activityId":            activity_id,
                "aerobicTrainingEffect": round(aerobic_te,   1),
                "anaerobicTrainingEffect": round(anaerobic_te, 1),
            },
        )
        log.info("Activity %s — aerobic TE %.1f  anaerobic TE %.1f",
                 activity_id, aerobic_te, anaerobic_te)
        return True
    except Exception as e:
        log.error("Failed to set training effect for %s: %s", activity_id, e)
        return False


def set_activity_type(activity_id: int, type_key: str) -> bool:
    """Update the activity type on Garmin Connect.

    Common type_key values:
      running           — outdoor running
      treadmill_running — treadmill (跑步機)
      indoor_cycling    — indoor cycling (室內自行車)
      cycling           — outdoor cycling
    """
    client = _get_client()
    try:
        client.connectapi(
            f"/activity-service/activity/{activity_id}",
            method="PUT",
            json={
                "activityId": activity_id,
                "activityTypeDTO": {"typeKey": type_key},
            },
        )
        log.info("Activity %s type set to '%s'", activity_id, type_key)
        return True
    except Exception as e:
        log.error("Failed to set activity type for %s: %s", activity_id, e)
        return False


def delete_activity(activity_id: int) -> bool:
    """Delete a Garmin Connect activity by ID. Returns True on success."""
    client = _get_client()
    try:
        client.connectapi(f"/activity-service/activity/{activity_id}", method="DELETE")
        log.info("Deleted activity %s", activity_id)
        return True
    except Exception as e:
        log.error("Failed to delete activity %s: %s", activity_id, e)
        return False
