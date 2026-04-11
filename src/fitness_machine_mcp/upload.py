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
