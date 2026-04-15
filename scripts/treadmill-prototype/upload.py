"""Upload activity TCX to Garmin Connect."""
from __future__ import annotations
import logging
from pathlib import Path
from garminconnect import Garmin

log = logging.getLogger("upload")

TOKEN_PATH = Path.home() / "ai-platform/mcps/garmin_mcp/data/tokens/casper"


def _get_client() -> Garmin:
    client = Garmin(is_cn=False)
    client.login(str(TOKEN_PATH))
    return client


def upload_activity(tcx_path: str) -> int | None:
    """Upload a TCX file to Garmin Connect. Returns activity ID."""
    client = _get_client()
    result = client.upload_activity(tcx_path)
    log.info("Upload result: %s", result)

    # Garmin returns {"detailedImportResult": {"successes": [{"internalId": 123}]}}
    try:
        return result["detailedImportResult"]["successes"][0]["internalId"]
    except (KeyError, IndexError, TypeError):
        log.warning("Could not extract activity ID from: %s", result)
        return None
