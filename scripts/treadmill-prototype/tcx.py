"""Generate TCX (Training Center XML) for a treadmill session."""
from __future__ import annotations
from datetime import datetime, timezone
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from engine import DataPoint


def build_tcx(plan: dict, data: list["DataPoint"], start_time: datetime) -> bytes:
    sport = plan.get("sport", "Running")
    total_secs   = data[-1].elapsed_sec if data else 0
    total_dist   = data[-1].distance_m  if data else 0.0
    avg_hr       = int(sum(d.hr_bpm for d in data if d.hr_bpm) /
                       max(1, sum(1 for d in data if d.hr_bpm)))
    max_hr       = max((d.hr_bpm for d in data), default=0)
    max_speed_ms = max((d.speed_kmh / 3.6 for d in data), default=0.0)
    calories     = int(total_secs * avg_hr * 0.0083)  # rough estimate

    start_iso = start_time.strftime("%Y-%m-%dT%H:%M:%SZ")

    trackpoints = []
    for d in data:
        ts = d.ts.strftime("%Y-%m-%dT%H:%M:%SZ")
        speed_ms = d.speed_kmh / 3.6
        run_cad  = d.cadence_spm // 2   # TCX RunCadence is per-foot (one side)

        hr_tag = f"<HeartRateBpm><Value>{d.hr_bpm}</Value></HeartRateBpm>" if d.hr_bpm else ""
        cad_tag = f"<Cadence>{run_cad}</Cadence>" if run_cad else ""
        ext_parts = []
        if speed_ms > 0:
            ext_parts.append(f"<Speed>{speed_ms:.3f}</Speed>")
        if run_cad:
            ext_parts.append(f"<RunCadence>{run_cad}</RunCadence>")
        ext_tag = (
            '<Extensions><TPX xmlns="http://www.garmin.com/xmlschemas/ActivityExtension/v2">'
            + "".join(ext_parts) + "</TPX></Extensions>"
        ) if ext_parts else ""

        trackpoints.append(f"""          <Trackpoint>
            <Time>{ts}</Time>
            <DistanceMeters>{d.distance_m:.1f}</DistanceMeters>
            {hr_tag}
            {cad_tag}
            {ext_tag}
          </Trackpoint>""")

    tp_xml = "\n".join(trackpoints)

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<TrainingCenterDatabase
  xmlns="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2"
  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
  xmlns:ns5="http://www.garmin.com/xmlschemas/ActivityGoals/v1"
  xmlns:ns3="http://www.garmin.com/xmlschemas/ActivityExtension/v2"
  xsi:schemaLocation="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2
    http://www.garmin.com/xmlschemas/TrainingCenterDatabasev2.xsd">
  <Activities>
    <Activity Sport="{sport}">
      <Id>{start_iso}</Id>
      <Lap StartTime="{start_iso}">
        <TotalTimeSeconds>{total_secs}</TotalTimeSeconds>
        <DistanceMeters>{total_dist:.1f}</DistanceMeters>
        <MaximumSpeed>{max_speed_ms:.3f}</MaximumSpeed>
        <Calories>{calories}</Calories>
        <AverageHeartRateBpm><Value>{avg_hr}</Value></AverageHeartRateBpm>
        <MaximumHeartRateBpm><Value>{max_hr}</Value></MaximumHeartRateBpm>
        <Intensity>Active</Intensity>
        <TriggerMethod>Manual</TriggerMethod>
        <Track>
{tp_xml}
        </Track>
      </Lap>
      <Creator xsi:type="Device_t">
        <Name>Casper Treadmill Controller (ADIDAS 1380)</Name>
        <UnitId>1380</UnitId>
        <ProductID>1380</ProductID>
        <Version><VersionMajor>1</VersionMajor><VersionMinor>0</VersionMinor></Version>
      </Creator>
    </Activity>
  </Activities>
</TrainingCenterDatabase>
"""
    return xml.encode("utf-8")
