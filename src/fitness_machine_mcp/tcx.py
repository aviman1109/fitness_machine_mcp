"""Generate TCX (Training Center XML) for a treadmill session."""
from __future__ import annotations
from datetime import datetime, timezone
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from fitness_machine_mcp.engine import DataPoint


def build_tcx(plan: dict, data: list["DataPoint"], start_time: datetime) -> bytes:
    sport_raw = plan.get("sport", "running").lower()
    # TCX Sport attribute values: "Running" or "Biking"
    sport_tcx = "Biking" if sport_raw == "cycling" else "Running"

    total_secs   = data[-1].elapsed_sec if data else 0
    total_dist   = data[-1].distance_m  if data else 0.0
    avg_hr       = int(sum(d.hr_bpm for d in data if d.hr_bpm) /
                       max(1, sum(1 for d in data if d.hr_bpm)))
    max_hr       = max((d.hr_bpm for d in data), default=0)
    max_speed_ms = max((d.speed_kmh / 3.6 for d in data), default=0.0)
    calories     = int(total_secs * avg_hr * 0.0083)  # rough estimate

    if sport_raw == "cycling":
        watts_series = [d.power_w_pm or d.power_w for d in data if (d.power_w_pm or d.power_w)]
        avg_watts = int(sum(watts_series) / len(watts_series)) if watts_series else 0
        max_watts = max(watts_series, default=0)

    start_iso = start_time.strftime("%Y-%m-%dT%H:%M:%SZ")

    trackpoints = []
    cumulative_altitude_m = 0.0
    prev_elapsed = 0
    for d in data:
        ts = d.ts.strftime("%Y-%m-%dT%H:%M:%SZ")
        speed_ms = d.speed_kmh / 3.6

        # Accumulate altitude from incline: Δalt = speed_m/s × (incline% / 100) × Δt
        dt = d.elapsed_sec - prev_elapsed
        if speed_ms > 0 and d.incline_pct != 0:
            cumulative_altitude_m += speed_ms * (d.incline_pct / 100.0) * dt
        prev_elapsed = d.elapsed_sec

        hr_tag  = f"<HeartRateBpm><Value>{d.hr_bpm}</Value></HeartRateBpm>" if d.hr_bpm else ""
        alt_tag = f"<AltitudeMeters>{cumulative_altitude_m:.2f}</AltitudeMeters>"
        ext_parts = []

        if sport_raw == "cycling":
            # Prefer pedal power meter; fall back to trainer-reported values
            power   = d.power_w_pm     or d.power_w
            cadence = d.cadence_rpm_pm or d.cadence_rpm
            if speed_ms > 0: ext_parts.append(f"<Speed>{speed_ms:.3f}</Speed>")
            if cadence:       ext_parts.append(f"<Cadence>{cadence}</Cadence>")
            if power:         ext_parts.append(f"<Watts>{power}</Watts>")
            if d.torque_eff_l:    ext_parts.append(f"<LeftTorqueEffectiveness>{d.torque_eff_l:.1f}</LeftTorqueEffectiveness>")
            if d.torque_eff_r:    ext_parts.append(f"<RightTorqueEffectiveness>{d.torque_eff_r:.1f}</RightTorqueEffectiveness>")
            if d.pedal_smooth_l:  ext_parts.append(f"<LeftPedalSmoothness>{d.pedal_smooth_l:.1f}</LeftPedalSmoothness>")
            if d.pedal_smooth_r:  ext_parts.append(f"<RightPedalSmoothness>{d.pedal_smooth_r:.1f}</RightPedalSmoothness>")
            cad_tag = f"<Cadence>{cadence}</Cadence>" if cadence else ""
        else:
            run_cad = d.cadence_spm // 2   # TCX RunCadence is per-foot (steps/min ÷ 2)
            if speed_ms > 0: ext_parts.append(f"<Speed>{speed_ms:.3f}</Speed>")
            if run_cad:       ext_parts.append(f"<RunCadence>{run_cad}</RunCadence>")
            cad_tag = f"<Cadence>{run_cad}</Cadence>" if run_cad else ""

        ext_tag = (
            '<Extensions><TPX xmlns="http://www.garmin.com/xmlschemas/ActivityExtension/v2">'
            + "".join(ext_parts) + "</TPX></Extensions>"
        ) if ext_parts else ""

        trackpoints.append(f"""          <Trackpoint>
            <Time>{ts}</Time>
            <DistanceMeters>{d.distance_m:.1f}</DistanceMeters>
            {alt_tag}
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
    <Activity Sport="{sport_tcx}">
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
        {f'''<Extensions><LX xmlns="http://www.garmin.com/xmlschemas/ActivityExtension/v2">
          <AvgWatts>{avg_watts}</AvgWatts>
          <MaxWatts>{max_watts}</MaxWatts>
        </LX></Extensions>''' if sport_raw == "cycling" else ""}
        <Track>
{tp_xml}
        </Track>
      </Lap>
      <Creator xsi:type="Device_t">
        <Name>{"eHuntSun T-SoX Trainer" if sport_raw == "cycling" else "ADIDAS 1380 Treadmill"}</Name>
        <UnitId>{"2250" if sport_raw == "cycling" else "1380"}</UnitId>
        <ProductID>{"2250" if sport_raw == "cycling" else "1380"}</ProductID>
        <Version><VersionMajor>1</VersionMajor><VersionMinor>0</VersionMinor></Version>
      </Creator>
    </Activity>
  </Activities>
</TrainingCenterDatabase>
"""
    return xml.encode("utf-8")
